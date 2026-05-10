"""CRUD + per-row test endpoints for notification_channels.

Mirrors the shape of ``oidc_providers.py`` so the admin UI can reuse
the same patterns: list rows, click a row to edit, click "+ Add" to
get a catalog dialog, click Test on any row to fire a one-shot probe.

Super admin only — same gate as the rest of /server/*.
"""
from __future__ import annotations
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..auth.crypto import encrypt
from ..deps import get_db, require_super_admin
from ..models import NotificationChannel, NotificationKind, User
from ..services import audit
from ..services.notifications import send_one

router = APIRouter(prefix="/server/notification-channels", tags=["notifications"])


# Allowlist of valid kinds — accepted on create/update. Mirrors the
# enum in models.py (the enum is the source of truth; this set just
# avoids a 500 when the operator sends a typo'd kind).
_VALID_KINDS = {k.value for k in NotificationKind}


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    name: str
    enabled: bool
    config: dict           # parsed JSON for UI rendering
    secret_set: bool       # never expose plaintext
    last_test_at: Optional[str] = None
    last_test_ok: Optional[bool] = None
    last_test_error: Optional[str] = None


def _to_out(ch: NotificationChannel) -> ChannelOut:
    try:
        cfg = json.loads(ch.config_json or "{}")
    except (json.JSONDecodeError, TypeError):
        cfg = {}
    return ChannelOut(
        id=ch.id,
        kind=ch.kind.value if hasattr(ch.kind, "value") else str(ch.kind),
        name=ch.name,
        enabled=bool(ch.enabled),
        config=cfg if isinstance(cfg, dict) else {},
        secret_set=bool(ch.secret_enc),
        last_test_at=ch.last_test_at.isoformat() + "Z" if ch.last_test_at else None,
        last_test_ok=ch.last_test_ok,
        last_test_error=ch.last_test_error,
    )


class ChannelCreate(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    config: dict = Field(default_factory=dict)
    secret: Optional[str] = None  # plaintext on the wire; encrypted at rest


class ChannelUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    enabled: Optional[bool] = None
    config: Optional[dict] = None
    secret: Optional[str] = None  # None = keep existing, "" = clear


def _coerce_kind(raw: str) -> NotificationKind:
    if raw not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown notification kind: {raw!r}")
    return NotificationKind(raw)


@router.get("", response_model=List[ChannelOut])
def list_channels(
    actor: User = Depends(require_super_admin), db: Session = Depends(get_db),
):
    rows = db.query(NotificationChannel).order_by(NotificationChannel.id).all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=ChannelOut)
def create_channel(
    req: ChannelCreate,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    kind = _coerce_kind(req.kind)
    row = NotificationChannel(
        kind=kind,
        name=req.name.strip(),
        enabled=bool(req.enabled),
        config_json=json.dumps(req.config or {}),
        secret_enc=encrypt(req.secret) if req.secret else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    audit.log(db, actor.id, "notification_channel_create",
              metadata={"id": row.id, "kind": kind.value, "name": row.name})
    return _to_out(row)


@router.patch("/{channel_id}", response_model=ChannelOut)
def update_channel(
    channel_id: int,
    req: ChannelUpdate,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = db.query(NotificationChannel).get(channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if req.name is not None:
        row.name = req.name.strip()
    if req.enabled is not None:
        row.enabled = bool(req.enabled)
    if req.config is not None:
        row.config_json = json.dumps(req.config or {})
    if req.secret is not None:
        # Empty string explicitly clears; non-empty re-encrypts; None
        # (default) means "leave the existing value alone" — same
        # convention as every other secret field in this app.
        row.secret_enc = encrypt(req.secret) if req.secret else None
    db.commit()
    db.refresh(row)
    audit.log(db, actor.id, "notification_channel_update",
              metadata={"id": row.id, "kind": row.kind.value})
    return _to_out(row)


@router.delete("/{channel_id}")
def delete_channel(
    channel_id: int,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = db.query(NotificationChannel).get(channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    db.delete(row)
    db.commit()
    audit.log(db, actor.id, "notification_channel_delete",
              metadata={"id": channel_id})
    return {"message": "Channel deleted."}


@router.post("/{channel_id}/test")
async def test_channel(
    channel_id: int,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = db.query(NotificationChannel).get(channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    ok, err = await send_one(db, row, "Vitriol — test notification.")
    audit.log(db, actor.id, "notification_channel_test",
              metadata={"id": channel_id, "ok": ok, "error": err})
    if not ok:
        raise HTTPException(status_code=502, detail=err or "Send failed")
    return {"message": "Test notification sent."}
