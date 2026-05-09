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
        allow_signup=s.allow_signup,
        signup_default_role=s.signup_default_role.value if hasattr(s.signup_default_role, "value") else str(s.signup_default_role),
        signup_default_custom_role_id=s.signup_default_custom_role_id,
        require_email_verification=bool(s.require_email_verification),
        smtp_host=s.smtp_host, smtp_port=s.smtp_port, smtp_user=s.smtp_user,
        smtp_from=s.smtp_from, smtp_use_tls=s.smtp_use_tls,
        smtp_password_set=bool(s.smtp_password_enc),
        discord_webhook_url=s.discord_webhook_url,
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
        super_admin_can_self_compile=s.super_admin_can_self_compile,
        admin_can_self_compile=s.admin_can_self_compile,
    )


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
    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port or 587,
            username=s.smtp_user or None,
            password=decrypt(s.smtp_password_enc) or None,
            start_tls=bool(s.smtp_use_tls),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SMTP delivery failed: {e}")
    audit.log(db, actor.id, "smtp_test", metadata={"to": to})
    return MessageResponse(message=f"Test email sent to {to}.")


@router.post("/refresh-certs", response_model=MessageResponse)
async def refresh_certs(actor: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    s = db.query(ServerSettings).get(1)
    if s is None or not s.ssl_cert_pull_webhook_url:
        raise HTTPException(status_code=400, detail="No SSL webhook configured")
    secret = decrypt(s.ssl_cert_pull_webhook_secret_enc) or ""
    payload = json.dumps({"action": "pull-certs"}).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest() if secret else ""
    headers = {"Content-Type": "application/json"}
    if sig:
        headers["X-Vitriol-Signature"] = f"sha256={sig}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(s.ssl_cert_pull_webhook_url, content=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Webhook call failed: {e}")
    cert_dir: Path = _cfg.cert_dir
    cert_dir.mkdir(parents=True, exist_ok=True)
    fullchain = data.get("fullchain")
    privkey = data.get("privkey")
    if not fullchain or not privkey:
        raise HTTPException(status_code=502, detail="Webhook response missing fullchain/privkey")
    (cert_dir / "fullchain.pem").write_text(fullchain, encoding="utf-8")
    (cert_dir / "privkey.pem").write_text(privkey, encoding="utf-8")
    audit.log(db, actor.id, "ssl_certs_refresh")
    return MessageResponse(message=f"Certificates written to {cert_dir}.")
