"""Database Provider CRUD + test / create-DB / initialize-schema flows.

Mirrors the NotificationChannel pattern: per-row config_json blob,
Fernet-encrypted secret, per-row test bookkeeping, super-admin only.
This is *infrastructure metadata* — saving a row here does **not**
switch the running app to that database. The operator still has to
point ``DATABASE_URL`` (or the active-db flag file, once that's wired)
at the new target and restart the container.

What this surface buys:

* A place to keep validated connection details across restarts so
  the operator doesn't have to re-type them.
* "Test connection" — opens a connection, runs ``SELECT 1``, stamps
  the row.
* "Create blank DB" — issues ``CREATE DATABASE`` on the target
  server. Idempotent (returns 200 if already there).
* "Initialize schema" — connects to the target and runs
  ``Base.metadata.create_all`` so the schema is laid out, ready for
  a future copy-tables migration.

The data-copy step itself is intentionally out of scope here.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..auth.crypto import decrypt, encrypt
from ..db import Base
from ..deps import get_db, require_super_admin
from ..models import DbProvider, User
from ..schemas import (
    DbProviderCreateRequest, DbProviderOut, DbProviderUpdateRequest, MessageResponse,
)
from ..services import audit
from ..services.db_url import build_url, known_kinds, server_url_for_create_db

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/server/db-providers", tags=["db-providers"])


# --------------------------------------------------------------- helpers


def _parse_config(blob: Optional[str]) -> dict:
    if not blob:
        return {}
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_out(p: DbProvider) -> DbProviderOut:
    return DbProviderOut(
        id=p.id,
        slug=p.slug,
        display_name=p.display_name,
        kind=p.kind,
        config=_parse_config(p.config_json),
        secret_set=bool(p.secret_enc),
        last_test_at=p.last_test_at,
        last_test_ok=p.last_test_ok,
        last_init_at=p.last_init_at,
        created_at=p.created_at,
    )


def _validate_kind(kind: str) -> str:
    kind = (kind or "").lower()
    if kind not in known_kinds():
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {sorted(known_kinds())}; got {kind!r}",
        )
    return kind


def _resolved_secret(p: DbProvider) -> Optional[str]:
    return decrypt(p.secret_enc) if p.secret_enc else None


# ---------------------------------------------------------------- CRUD


@router.get("", response_model=List[DbProviderOut])
def list_providers(actor: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    rows = db.query(DbProvider).order_by(DbProvider.id).all()
    return [_to_out(p) for p in rows]


@router.post("", response_model=DbProviderOut)
def create_provider(
    req: DbProviderCreateRequest,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    kind = _validate_kind(req.kind)
    if db.query(DbProvider).filter(DbProvider.slug == req.slug).count():
        raise HTTPException(status_code=409, detail="A provider with that slug already exists.")
    p = DbProvider(
        slug=req.slug,
        display_name=req.display_name,
        kind=kind,
        config_json=json.dumps(req.config or {}),
        secret_enc=encrypt(req.secret) if req.secret else None,
        created_by_user_id=actor.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    audit.log(db, actor.id, "db_provider_create",
              metadata={"id": p.id, "slug": p.slug, "kind": kind})
    return _to_out(p)


@router.patch("/{provider_id}", response_model=DbProviderOut)
def update_provider(
    provider_id: int, req: DbProviderUpdateRequest,
    actor: User = Depends(require_super_admin), db: Session = Depends(get_db),
):
    p: Optional[DbProvider] = db.query(DbProvider).get(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if req.display_name is not None:
        p.display_name = req.display_name
    if req.kind is not None:
        p.kind = _validate_kind(req.kind)
    if req.config is not None:
        p.config_json = json.dumps(req.config)
    if req.secret is not None:
        # Empty string explicitly clears the saved secret; non-empty
        # replaces it. Null (not passed at all) leaves the existing
        # encrypted blob alone.
        p.secret_enc = encrypt(req.secret) if req.secret else None
    db.commit()
    db.refresh(p)
    audit.log(db, actor.id, "db_provider_update", metadata={"id": p.id})
    return _to_out(p)


@router.delete("/{provider_id}", response_model=MessageResponse)
def delete_provider(
    provider_id: int,
    actor: User = Depends(require_super_admin), db: Session = Depends(get_db),
):
    p: Optional[DbProvider] = db.query(DbProvider).get(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    slug = p.slug
    db.delete(p)
    db.commit()
    audit.log(db, actor.id, "db_provider_delete", metadata={"id": provider_id, "slug": slug})
    return MessageResponse(message=f"Provider {slug!r} deleted.")


# --------------------------------------------- test / create / initialize


@router.post("/{provider_id}/test", response_model=DbProviderOut)
def test_provider(
    provider_id: int,
    actor: User = Depends(require_super_admin), db: Session = Depends(get_db),
):
    p: Optional[DbProvider] = db.query(DbProvider).get(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    cfg = _parse_config(p.config_json)
    secret = _resolved_secret(p)
    try:
        url = build_url(p.kind, cfg, secret)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    err: Optional[str] = None
    try:
        engine = create_engine(url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except (SQLAlchemyError, OSError, Exception) as e:   # broad on purpose — driver errors are unhashable
        err = f"{type(e).__name__}: {e}"
        _log.info("db_provider test failed for id=%s: %s", provider_id, err)
    p.last_test_at = datetime.utcnow()
    p.last_test_ok = err is None
    db.commit()
    db.refresh(p)
    audit.log(db, actor.id, "db_provider_test",
              metadata={"id": provider_id, "ok": p.last_test_ok, "error": err})
    if err is not None:
        # Surface the error so the UI can show it inline. 200 with the
        # stamped row would hide a useful message; mirror the SMTP/SSO
        # test endpoints which 400 on failure.
        raise HTTPException(status_code=400, detail=err)
    return _to_out(p)


@router.post("/{provider_id}/create-db", response_model=DbProviderOut)
def create_target_db(
    provider_id: int,
    actor: User = Depends(require_super_admin), db: Session = Depends(get_db),
):
    """Issue ``CREATE DATABASE`` against the *server-level* database
    using the saved credentials. Idempotent — already-existing target
    returns 200 with a note in the audit log."""
    p: Optional[DbProvider] = db.query(DbProvider).get(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    cfg = _parse_config(p.config_json)
    secret = _resolved_secret(p)
    kind = (p.kind or "").lower()

    if kind == "sqlite":
        # The file gets created by SQLAlchemy the first time it's
        # opened — there's nothing to "create" up front. Just open +
        # close once so the file exists on disk.
        try:
            url = build_url(kind, cfg, secret)
            engine = create_engine(url, future=True)
            with engine.connect():
                pass
            engine.dispose()
        except SQLAlchemyError as e:
            raise HTTPException(status_code=400, detail=str(e))
        audit.log(db, actor.id, "db_provider_create_db",
                  metadata={"id": provider_id, "kind": kind})
        return _to_out(p)

    db_name = (cfg.get("db_name") or "").strip()
    if not db_name:
        raise HTTPException(status_code=400, detail="db_name is required for create-db")
    if not all(c.isalnum() or c in "_-" for c in db_name):
        # Defense against accidental SQL injection — db_name comes from
        # user input and CREATE DATABASE doesn't accept bind params.
        raise HTTPException(status_code=400,
                            detail="db_name may only contain letters, digits, _ and -.")

    try:
        server_url = server_url_for_create_db(kind, cfg, secret)
        engine = create_engine(server_url, future=True, isolation_level="AUTOCOMMIT")
    except (ValueError, SQLAlchemyError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    note: Optional[str] = None
    try:
        with engine.connect() as conn:
            # Dialect-specific "create if not exists" logic. Postgres /
            # MySQL / MariaDB all support IF NOT EXISTS; MSSQL needs a
            # conditional check via T-SQL.
            if kind in ("postgres",):
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"),
                    {"n": db_name},
                ).scalar()
                if exists:
                    note = "already exists"
                else:
                    conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            elif kind in ("mysql", "mariadb"):
                conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}`"))
            elif kind == "mssql":
                exists = conn.execute(
                    text("SELECT 1 FROM sys.databases WHERE name = :n"),
                    {"n": db_name},
                ).scalar()
                if exists:
                    note = "already exists"
                else:
                    conn.execute(text(f"CREATE DATABASE [{db_name}]"))
            else:
                raise HTTPException(status_code=400, detail=f"create-db not supported for kind={kind!r}")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
    finally:
        engine.dispose()

    audit.log(db, actor.id, "db_provider_create_db",
              metadata={"id": provider_id, "kind": kind, "db": db_name, "note": note})
    return _to_out(p)


@router.post("/{provider_id}/initialize-schema", response_model=DbProviderOut)
def initialize_schema(
    provider_id: int,
    actor: User = Depends(require_super_admin), db: Session = Depends(get_db),
):
    """Connect to the target and run ``Base.metadata.create_all``.

    Creates every Vitriol table on the target with an empty payload.
    Does **not** copy any data over — that's a future step.
    """
    p: Optional[DbProvider] = db.query(DbProvider).get(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    cfg = _parse_config(p.config_json)
    secret = _resolved_secret(p)
    try:
        url = build_url(p.kind, cfg, secret)
        engine = create_engine(url, future=True)
        Base.metadata.create_all(bind=engine)
        engine.dispose()
    except (SQLAlchemyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
    p.last_init_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    audit.log(db, actor.id, "db_provider_init_schema",
              metadata={"id": provider_id, "kind": p.kind})
    return _to_out(p)
