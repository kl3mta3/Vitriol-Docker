"""Tips CRUD — super-admin only.

Tips are short text lines shown randomly on the main app page below the
status bar.  Endpoints here let the operator add, delete, import, and
export tips.  The `tips_enabled` toggle lives on `ServerSettings` and is
patched via the normal PATCH /server/settings endpoint.
"""
from __future__ import annotations

import json
from typing import List

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from fastapi.routing import APIRouter

from ..deps import get_db, require_super_admin
from ..models import Tip, User
from ..schemas import MessageResponse, TipCreate, TipImport, TipOut, TipUpdate

from sqlalchemy.orm import Session

router = APIRouter(prefix="/server/tips", tags=["tips"])


@router.get("", response_model=List[TipOut])
async def list_tips(
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return db.query(Tip).order_by(Tip.created_at.asc()).all()


@router.post("", response_model=TipOut, status_code=201)
async def create_tip(
    payload: TipCreate,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    tip = Tip(body=payload.body.strip())
    db.add(tip)
    db.commit()
    db.refresh(tip)
    return tip


@router.patch("/{tip_id}", response_model=TipOut)
async def update_tip(
    tip_id: int,
    payload: TipUpdate,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    tip = db.query(Tip).get(tip_id)
    if tip is None:
        raise HTTPException(status_code=404, detail="Tip not found.")
    tip.body = payload.body.strip()
    db.commit()
    db.refresh(tip)
    return tip


@router.delete("/{tip_id}", response_model=MessageResponse)
async def delete_tip(
    tip_id: int,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    tip = db.query(Tip).get(tip_id)
    if tip is None:
        raise HTTPException(status_code=404, detail="Tip not found.")
    db.delete(tip)
    db.commit()
    return MessageResponse(message="Tip deleted.")


@router.delete("", response_model=MessageResponse)
async def clear_tips(
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Delete every tip in one shot."""
    deleted = db.query(Tip).delete()
    db.commit()
    return MessageResponse(message=f"Cleared {deleted} tip(s).")


@router.post("/import", response_model=MessageResponse)
async def import_tips(
    payload: TipImport,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Bulk-import tips from a JSON list.

    When ``replace=true`` all existing tips are deleted first.  Lines that
    are blank after stripping are skipped.
    """
    if payload.replace:
        db.query(Tip).delete()
    added = 0
    for line in payload.tips:
        text = str(line).strip()
        if not text:
            continue
        if len(text) > 500:
            text = text[:500]
        db.add(Tip(body=text))
        added += 1
    db.commit()
    verb = "Replaced all tips with" if payload.replace else "Added"
    return MessageResponse(message=f"{verb} {added} tip(s).")


@router.get("/export")
async def export_tips(
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Download all tips as a JSON file."""
    tips = db.query(Tip).order_by(Tip.created_at.asc()).all()
    data = json.dumps({"tips": [t.body for t in tips]}, ensure_ascii=False, indent=2)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="vitriol-tips.json"'},
    )
