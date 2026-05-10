"""Super-admin server settings, restart, SSL cert refresh."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
import signal
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.crypto import decrypt, encrypt
from ..auth.permissions import is_admin_or_above
from ..config import get_settings
from ..deps import get_current_user, get_db, require_admin, require_super_admin
from ..models import ServerSettings, User
from ..schemas import MessageResponse, ServerSettingsOut, ServerSettingsPatch
from ..services import audit

router = APIRouter(prefix="/server", tags=["server"])
_cfg = get_settings()


def _to_out(s: ServerSettings) -> ServerSettingsOut:
    return ServerSettingsOut(
        bind_host=s.bind_host, bind_port=s.bind_port,
        global_rate_limit_per_minute=s.global_rate_limit_per_minute,
        max_file_size_bytes=s.max_file_size_bytes,
        default_user_daily_limit=s.default_user_daily_limit,
        default_user_rate_limit=s.default_user_rate_limit,
        default_admin_daily_limit=s.default_admin_daily_limit,
        default_admin_rate_limit=s.default_admin_rate_limit,
        disabled_input_formats_json=s.disabled_input_formats_json,
        disabled_output_formats_json=s.disabled_output_formats_json,
        disabled_admin_input_formats_json=s.disabled_admin_input_formats_json or "[]",
        disabled_admin_output_formats_json=s.disabled_admin_output_formats_json or "[]",
        disabled_user_input_formats_json=s.disabled_user_input_formats_json or "[]",
        disabled_user_output_formats_json=s.disabled_user_output_formats_json or "[]",
        allow_signup=s.allow_signup,
        signup_default_role=s.signup_default_role.value if hasattr(s.signup_default_role, "value") else str(s.signup_default_role),
        signup_default_custom_role_id=s.signup_default_custom_role_id,
        require_email_verification=bool(s.require_email_verification),
        smtp_host=s.smtp_host, smtp_port=s.smtp_port, smtp_user=s.smtp_user,
        smtp_from=s.smtp_from, smtp_use_tls=s.smtp_use_tls,
        smtp_password_set=bool(s.smtp_password_enc),
        smtp_last_test_at=s.smtp_last_test_at,
        smtp_last_test_ok=s.smtp_last_test_ok,
        discord_webhook_url=s.discord_webhook_url,
        discord_last_test_at=s.discord_last_test_at,
        discord_last_test_ok=s.discord_last_test_ok,
        oauth_google_client_id=s.oauth_google_client_id,
        oauth_google_secret_set=bool(s.oauth_google_client_secret_enc),
        oauth_github_client_id=s.oauth_github_client_id,
        oauth_github_secret_set=bool(s.oauth_github_client_secret_enc),
        oidc_enabled=bool(s.oidc_enabled),
        oidc_display_name=s.oidc_display_name,
        oidc_issuer=s.oidc_issuer,
        oidc_client_id=s.oidc_client_id,
        oidc_secret_set=bool(s.oidc_client_secret_enc),
        oidc_scopes=s.oidc_scopes or "openid email profile",
        public_base_url=s.public_base_url,
        allowed_origin=s.allowed_origin,
        ssl_cert_pull_webhook_url=s.ssl_cert_pull_webhook_url,
        ssl_cert_pull_webhook_secret_set=bool(s.ssl_cert_pull_webhook_secret_enc),
        ssl_cert_pull_mode=s.ssl_cert_pull_mode or "webhook",
        ssl_cert_pull_script=s.ssl_cert_pull_script,
        ssl_cert_pull_auto_days=int(s.ssl_cert_pull_auto_days or 0),
        ssl_cert_pull_last_run_at=s.ssl_cert_pull_last_run_at,
        ssl_cert_pull_last_status=s.ssl_cert_pull_last_status,
        ssl_cert_pull_webhook_method=s.ssl_cert_pull_webhook_method or "POST",
        ssl_cert_pull_webhook_header_name=s.ssl_cert_pull_webhook_header_name,
        ssl_cert_pull_webhook_header_value_set=bool(s.ssl_cert_pull_webhook_header_value_enc),
        ssl_cert_pull_response_cert_field=s.ssl_cert_pull_response_cert_field or "fullchain",
        ssl_cert_pull_response_key_field=s.ssl_cert_pull_response_key_field or "privkey",
        super_admin_can_self_compile=s.super_admin_can_self_compile,
        admin_can_self_compile=s.admin_can_self_compile,
        output_retention=_load_retention(s),
    )


def _load_retention(s: ServerSettings) -> dict:
    """Parse the per-role retention JSON, falling back to safe defaults
    if the column is missing/empty/malformed."""
    defaults = {
        "super_admin": {"max_files": 0, "max_age": 0, "age_unit": "days", "delete_on_download": False},
        "admin":       {"max_files": 0, "max_age": 30, "age_unit": "days", "delete_on_download": False},
        "user":        {"max_files": 20, "max_age": 24, "age_unit": "hours", "delete_on_download": False},
    }
    raw = (s.output_retention_json or "").strip()
    if not raw:
        return defaults
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return defaults
    # Merge so partial saves don't lose roles.
    out = dict(defaults)
    for role, cfg in parsed.items():
        if not isinstance(cfg, dict):
            continue
        merged = dict(defaults.get(role, {}))
        merged.update(cfg)
        out[role] = merged
    return out


@router.get("/settings", response_model=ServerSettingsOut)
def get_server_settings(actor: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    s = db.query(ServerSettings).get(1)
    if s is None:
        raise HTTPException(status_code=500, detail="Settings row missing")
    return _to_out(s)


@router.patch("/settings", response_model=ServerSettingsOut)
def patch_server_settings(
    req: ServerSettingsPatch,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    s = db.query(ServerSettings).get(1)
    if s is None:
        raise HTTPException(status_code=500, detail="Settings row missing")

    plain_fields = {
        # bind_host / bind_port are intentionally NOT here. The container's
        # listener is set by the Dockerfile CMD, and the public address by
        # the orchestrator (Coolify domain / docker compose ports / reverse
        # proxy) — neither path reads from this table. Patching them
        # silently does nothing, which is worse UX than rejecting them.
        "global_rate_limit_per_minute", "max_file_size_bytes",
        "default_user_daily_limit", "default_user_rate_limit",
        "default_admin_daily_limit", "default_admin_rate_limit",
        "allow_signup",
        "require_email_verification",
        "smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_use_tls",
        "discord_webhook_url",
        "oauth_google_client_id", "oauth_github_client_id",
        "oidc_enabled", "oidc_display_name", "oidc_issuer", "oidc_client_id",
        "oidc_scopes",
        "public_base_url", "allowed_origin",
        "ssl_cert_pull_webhook_url",
        "ssl_cert_pull_mode", "ssl_cert_pull_script", "ssl_cert_pull_auto_days",
        "ssl_cert_pull_webhook_method", "ssl_cert_pull_webhook_header_name",
        "ssl_cert_pull_response_cert_field", "ssl_cert_pull_response_key_field",
        "super_admin_can_self_compile", "admin_can_self_compile",
    }
    for f in plain_fields:
        v = getattr(req, f)
        if v is not None:
            setattr(s, f, v)

    if req.signup_default_role is not None:
        s.signup_default_role = req.signup_default_role  # SQLAlchemy enum coercion handles string
    if req.signup_default_custom_role_id is not None:
        # 0 clears, positive int assigns (validated against the table).
        from ..models import CustomRole as _CR
        if req.signup_default_custom_role_id == 0:
            s.signup_default_custom_role_id = None
        else:
            cr = db.query(_CR).get(req.signup_default_custom_role_id)
            if cr is None:
                raise HTTPException(status_code=400, detail="signup_default_custom_role_id does not exist.")
            s.signup_default_custom_role_id = cr.id
            # Keep the builtin role aligned with the custom role's ceiling so
            # signup() reads consistent values.
            s.signup_default_role = cr.base_role

    if req.disabled_input_formats is not None:
        s.disabled_input_formats_json = json.dumps(req.disabled_input_formats)
    if req.disabled_output_formats is not None:
        s.disabled_output_formats_json = json.dumps(req.disabled_output_formats)
    if req.disabled_admin_input_formats is not None:
        s.disabled_admin_input_formats_json = json.dumps(req.disabled_admin_input_formats)
    if req.disabled_admin_output_formats is not None:
        s.disabled_admin_output_formats_json = json.dumps(req.disabled_admin_output_formats)
    if req.disabled_user_input_formats is not None:
        s.disabled_user_input_formats_json = json.dumps(req.disabled_user_input_formats)
    if req.disabled_user_output_formats is not None:
        s.disabled_user_output_formats_json = json.dumps(req.disabled_user_output_formats)
    if req.output_retention is not None:
        # Sanitize incoming structure — accept only known roles + fields,
        # coerce types so a misbehaving frontend can't poison the JSON.
        valid_units = {"minutes", "hours", "days"}
        sanitized: dict[str, dict] = {}
        for role, cfg in (req.output_retention or {}).items():
            if role not in {"super_admin", "admin", "user"} or not isinstance(cfg, dict):
                continue
            sanitized[role] = {
                "max_files": max(0, int(cfg.get("max_files", 0) or 0)),
                "max_age":   max(0, int(cfg.get("max_age", 0) or 0)),
                "age_unit":  cfg.get("age_unit") if cfg.get("age_unit") in valid_units else "days",
                "delete_on_download": bool(cfg.get("delete_on_download", False)),
            }
        s.output_retention_json = json.dumps(sanitized)

    if req.smtp_password is not None:
        s.smtp_password_enc = encrypt(req.smtp_password)
    if req.oauth_google_client_secret is not None:
        s.oauth_google_client_secret_enc = encrypt(req.oauth_google_client_secret)
    if req.oauth_github_client_secret is not None:
        s.oauth_github_client_secret_enc = encrypt(req.oauth_github_client_secret)
    if req.oidc_client_secret is not None:
        s.oidc_client_secret_enc = encrypt(req.oidc_client_secret)
    if req.ssl_cert_pull_webhook_secret is not None:
        s.ssl_cert_pull_webhook_secret_enc = encrypt(req.ssl_cert_pull_webhook_secret)
    if req.ssl_cert_pull_webhook_header_value is not None:
        # Empty string explicitly clears the header auth.
        s.ssl_cert_pull_webhook_header_value_enc = encrypt(req.ssl_cert_pull_webhook_header_value) if req.ssl_cert_pull_webhook_header_value else None

    s.updated_by_user_id = actor.id
    db.commit()
    db.refresh(s)
    audit.log(db, actor.id, "server_settings_update")
    return _to_out(s)


@router.get("/secret-key")
def get_secret_key(actor: User = Depends(require_super_admin)):
    """Reveal the current server secret key. Super admin only.

    Used to view the auto-generated value on first install (so the admin
    can stash it somewhere safe). Has no side effects.
    """
    return {"secret_key": _cfg.secret_key}


@router.post("/secret-key/rotate", response_model=MessageResponse)
def rotate_secret_key(
    background: BackgroundTasks,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Generate a new secret key, re-encrypt at-rest secrets, restart.

    Restart is required so JWT signing + the in-memory Fernet pick up
    the new key. All sessions are invalidated; the operator will land on
    /signin afterward.
    """
    from ..services.key_rotation import rotate as do_rotate
    try:
        do_rotate(db)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.log(db, actor.id, "secret_key_rotate")
    # Defer restart so the HTTP response can flush before the process dies.
    import os, signal
    background.add_task(lambda: os.kill(os.getpid(), signal.SIGTERM))
    return MessageResponse(message="Secret key rotated. Server restarting — sign in again in a few seconds.")


class _ExportRequest(BaseModel):
    password: Optional[str] = None


class _ImportRequest(BaseModel):
    envelope: dict
    password: Optional[str] = None
    confirm: bool = False


@router.post("/export")
def export_settings(
    req: _ExportRequest,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Bundle all server-side configuration into a portable JSON file.

    With a password, the body is encrypted with PBKDF2-derived Fernet
    so the resulting file is safe to email / drop in a backup. Without,
    secrets are emitted in plaintext (fine for an internal admin who's
    immediately moving it to another vault).
    """
    from ..services import settings_export as _se
    snapshot = _se.collect(db)
    envelope = _se.make_envelope(snapshot, req.password or None)
    audit.log(db, actor.id, "settings_export", metadata={"encrypted": bool(req.password)})
    return envelope


@router.post("/import")
def import_settings(
    req: _ImportRequest,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Two-step import: with `confirm=false` we parse + validate +
    decrypt + verify hash, then return a small summary so the UI can
    show a confirmation modal. With `confirm=true` we apply.

    On password mismatch or hash mismatch we return 400 — the messages
    are intentionally vague so a wrong password can't be distinguished
    from a corrupted file (no oracle).
    """
    from ..services import settings_export as _se
    try:
        plaintext = _se.parse_envelope(req.envelope, req.password)
    except _se.NeedsPasswordError:
        # Specific 401-shaped response so the UI can switch to "ask for
        # password" without showing a scary error.
        raise HTTPException(status_code=401, detail={"needs_password": True})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    summary = _se.summarize(plaintext)
    if not req.confirm:
        return {"summary": summary, "ready": True}

    applied = _se.apply(db, plaintext)
    audit.log(db, actor.id, "settings_import", metadata=applied)
    return {"summary": summary, "applied": applied, "message": "Settings imported."}


@router.post("/restart", response_model=MessageResponse)
def restart(actor: User = Depends(require_admin), background: BackgroundTasks = None, db: Session = Depends(get_db)):
    audit.log(db, actor.id, "server_restart")

    def _later():
        # Send SIGTERM to the master process; container orchestrator
        # (or `uvicorn --reload` in dev) will bring it back up.
        os.kill(os.getpid(), signal.SIGTERM)

    if background is not None:
        background.add_task(_later)
    return MessageResponse(message="Restart scheduled.")


@router.post("/test-email", response_model=MessageResponse)
async def test_email(
    payload: dict,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Send a test email through the saved SMTP settings.

    Helps the super admin verify SMTP works without waiting for a real
    sign-up. Returns 400 if SMTP isn't configured, 502 if delivery fails.
    """
    s = db.query(ServerSettings).get(1)
    if s is None or not s.smtp_host or not s.smtp_from:
        raise HTTPException(status_code=400, detail="SMTP is not configured. Save Host, Port, From, and (if needed) credentials first.")
    to = (payload or {}).get("to") or s.smtp_from
    if "@" not in str(to):
        raise HTTPException(status_code=400, detail="Recipient address looks invalid.")

    # Build the message inline rather than reusing email_svc helpers — this
    # is a one-shot test, no token issuance, no DB writes.
    from email.message import EmailMessage
    import aiosmtplib
    from ..auth.crypto import decrypt

    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = "Vitriol — SMTP test"
    msg.set_content(
        "If you can read this, SMTP is wired up correctly.\n\n"
        f"Sent by Vitriol via {s.smtp_host}:{s.smtp_port or 587} as {s.smtp_user or '(no auth)'}.\n"
    )
    from datetime import datetime as _dt
    try:
        # Hard 15s budget for the entire SMTP round trip. aiosmtplib's
        # default is 60s for both connection and per-command timeouts;
        # behind a Cloudflare/Coolify proxy chain that's longer than the
        # proxy's own request timeout, so a hung SMTP server (purelymail
        # rate-limit, network glitch, etc.) surfaces as "502 Bad Gateway"
        # with no actionable detail. 15s lets us beat the proxy and
        # return a real error message.
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port or 587,
            username=s.smtp_user or None,
            password=decrypt(s.smtp_password_enc) or None,
            start_tls=bool(s.smtp_use_tls),
            timeout=15,
        )
    except Exception as e:
        s.smtp_last_test_at = _dt.utcnow()
        s.smtp_last_test_ok = False
        db.commit()
        # Include exception class in the detail so 421/535/timeout look
        # different in the UI. Helps the operator distinguish auth fail
        # vs. rate-limit vs. network hang vs. STARTTLS failure.
        raise HTTPException(
            status_code=502,
            detail=f"SMTP delivery failed ({type(e).__name__}): {e}",
        )
    s.smtp_last_test_at = _dt.utcnow()
    s.smtp_last_test_ok = True
    db.commit()
    audit.log(db, actor.id, "smtp_test", metadata={"to": to})
    return MessageResponse(message=f"Test email sent to {to}.")


@router.post("/test-discord", response_model=MessageResponse)
async def test_discord(
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Send a one-off message to the configured Discord webhook so the
    super admin can verify their URL works without waiting for a real
    pending-approval event."""
    s = db.query(ServerSettings).get(1)
    if s is None or not s.discord_webhook_url:
        raise HTTPException(status_code=400, detail="Discord webhook URL is empty.")
    import httpx as _httpx
    from datetime import datetime as _dt
    payload = {
        "content": (
            ":zap: Vitriol test message — your Discord integration is working. "
            "(This was triggered manually from the Server settings page.)"
        )
    }
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(s.discord_webhook_url, json=payload)
            r.raise_for_status()
    except Exception as e:
        s.discord_last_test_at = _dt.utcnow()
        s.discord_last_test_ok = False
        db.commit()
        raise HTTPException(status_code=502, detail=f"Discord post failed: {e}")
    s.discord_last_test_at = _dt.utcnow()
    s.discord_last_test_ok = True
    db.commit()
    audit.log(db, actor.id, "discord_test")
    return MessageResponse(message="Discord test message posted.")


@router.post("/refresh-certs", response_model=MessageResponse)
async def refresh_certs(actor: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    """Trigger a cert pull immediately. Dispatches on `ssl_cert_pull_mode`
    (webhook or script) — see web/services/cert_pull.py for the modes."""
    from ..services.cert_pull import run as do_pull, CertPullError
    try:
        msg = await do_pull(db)
    except CertPullError as e:
        raise HTTPException(status_code=502, detail=str(e))
    audit.log(db, actor.id, "ssl_certs_refresh", metadata={"status": msg})
    return MessageResponse(message=msg)
