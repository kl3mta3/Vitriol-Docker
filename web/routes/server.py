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
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
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
        max_output_size_bytes=int(getattr(s, "max_output_size_bytes", 0) or 0),
        max_storage_bytes=int(getattr(s, "max_storage_bytes", 0) or 0),
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
        require_name_at_signup=bool(getattr(s, "require_name_at_signup", False)),
        password_reset_via_email=bool(getattr(s, "password_reset_via_email", False)),
        password_signin_enabled=bool(s.password_signin_enabled),
        smtp_enabled=bool(s.smtp_enabled),
        smtp_host=s.smtp_host, smtp_port=s.smtp_port, smtp_user=s.smtp_user,
        smtp_from=s.smtp_from, smtp_use_tls=s.smtp_use_tls,
        smtp_password_set=bool(s.smtp_password_enc),
        smtp_last_test_at=s.smtp_last_test_at,
        smtp_last_test_ok=s.smtp_last_test_ok,
        discord_webhook_url=s.discord_webhook_url,
        discord_last_test_at=s.discord_last_test_at,
        discord_last_test_ok=s.discord_last_test_ok,
        oauth_google_enabled=bool(s.oauth_google_enabled),
        oauth_google_show_on_signup=bool(getattr(s, "oauth_google_show_on_signup", True)),
        oauth_google_client_id=s.oauth_google_client_id,
        oauth_google_secret_set=bool(s.oauth_google_client_secret_enc),
        oauth_google_last_test_at=s.oauth_google_last_test_at,
        oauth_google_last_test_ok=s.oauth_google_last_test_ok,
        oauth_github_enabled=bool(s.oauth_github_enabled),
        oauth_github_show_on_signup=bool(getattr(s, "oauth_github_show_on_signup", True)),
        oauth_github_client_id=s.oauth_github_client_id,
        oauth_github_secret_set=bool(s.oauth_github_client_secret_enc),
        oauth_github_last_test_at=s.oauth_github_last_test_at,
        oauth_github_last_test_ok=s.oauth_github_last_test_ok,
        oidc_enabled=bool(s.oidc_enabled),
        oidc_display_name=s.oidc_display_name,
        oidc_issuer=s.oidc_issuer,
        oidc_client_id=s.oidc_client_id,
        oidc_secret_set=bool(s.oidc_client_secret_enc),
        oidc_scopes=s.oidc_scopes or "openid email profile",
        public_base_url=s.public_base_url,
        allowed_origin=s.allowed_origin,
        brand_title=getattr(s, "brand_title", None),
        brand_link=getattr(s, "brand_link", None),
        brand_logo_set=bool(getattr(s, "brand_logo_filename", None)),
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
        storage_backend=(s.storage_backend or "local"),
        s3_endpoint_url=s.s3_endpoint_url,
        s3_bucket=s.s3_bucket,
        s3_region=s.s3_region,
        s3_access_key=s.s3_access_key,
        s3_secret_key_set=bool(s.s3_secret_key_enc),
        s3_path_prefix=s.s3_path_prefix,
        s3_force_path_style=bool(s.s3_force_path_style),
        s3_last_test_at=s.s3_last_test_at,
        s3_last_test_ok=s.s3_last_test_ok,
        max_concurrent_conversions=int(s.max_concurrent_conversions or 3),
        streaming_safety_divisor=int(s.streaming_safety_divisor or 4),
        queue_weight_super_admin=int(getattr(s, "queue_weight_super_admin", None) or 5),
        queue_weight_admin=int(getattr(s, "queue_weight_admin", None) or 3),
        queue_weight_user=int(getattr(s, "queue_weight_user", None) or 1),
        server_show_border=bool(getattr(s, "server_show_border", True)),
        server_border_style=str(getattr(s, "server_border_style", None) or "vitriol"),
        server_default_theme=getattr(s, "server_default_theme", None) or None,
        server_allow_user_theme=bool(getattr(s, "server_allow_user_theme", True)),
        support_email=getattr(s, "support_email", None) or None,
        support_discord_url=getattr(s, "support_discord_url", None) or None,
        support_discord_shared=bool(getattr(s, "support_discord_shared", False)),
        show_limit_hit_notice=bool(getattr(s, "show_limit_hit_notice", True)),
        show_user_support_button=bool(getattr(s, "show_user_support_button", False)),
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
        "max_output_size_bytes", "max_storage_bytes",
        "default_user_daily_limit", "default_user_rate_limit",
        "default_admin_daily_limit", "default_admin_rate_limit",
        "allow_signup",
        "require_email_verification",
        "require_name_at_signup",
        "password_reset_via_email",
        "password_signin_enabled",
        "smtp_enabled",
        "smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_use_tls",
        "discord_webhook_url",
        "oauth_google_enabled", "oauth_google_show_on_signup", "oauth_google_client_id",
        "oauth_github_enabled", "oauth_github_show_on_signup", "oauth_github_client_id",
        "oidc_enabled", "oidc_display_name", "oidc_issuer", "oidc_client_id",
        "oidc_scopes",
        "public_base_url", "allowed_origin",
        "brand_title", "brand_link",
        "ssl_cert_pull_webhook_url",
        "ssl_cert_pull_mode", "ssl_cert_pull_script", "ssl_cert_pull_auto_days",
        "ssl_cert_pull_webhook_method", "ssl_cert_pull_webhook_header_name",
        "ssl_cert_pull_response_cert_field", "ssl_cert_pull_response_key_field",
        "super_admin_can_self_compile", "admin_can_self_compile",
        # Storage backend selection + non-secret S3 config. The secret
        # is encrypt-on-write below; everything else is a plain set.
        "storage_backend",
        "s3_endpoint_url", "s3_bucket", "s3_region", "s3_access_key",
        "s3_path_prefix", "s3_force_path_style",
        # Performance knobs. Both clamped at the route layer below
        # rather than as Pydantic validators so the column accepts
        # historical out-of-range values (defensive — the column
        # default is in the safe range).
        "max_concurrent_conversions", "streaming_safety_divisor",
        "queue_weight_super_admin", "queue_weight_admin", "queue_weight_user",
        # Server-level border control
        "server_show_border", "server_border_style",
        # Site-wide theme defaults
        "server_default_theme", "server_allow_user_theme",
        # Support contact
        "support_email", "support_discord_url", "support_discord_shared",
        "show_limit_hit_notice", "show_user_support_button",
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
    if req.s3_secret_key is not None:
        # Empty string explicitly clears the saved key (operator deleting
        # creds); non-empty replaces it.
        s.s3_secret_key_enc = encrypt(req.s3_secret_key) if req.s3_secret_key else None

    # Clamp the perf knobs into safe ranges so a manually crafted PATCH
    # can't push us into a runaway-thread or zero-RAM-budget state.
    if s.max_concurrent_conversions is not None:
        s.max_concurrent_conversions = max(1, min(16, int(s.max_concurrent_conversions)))
    if s.streaming_safety_divisor is not None:
        s.streaming_safety_divisor = max(2, min(8, int(s.streaming_safety_divisor)))
    for _wf in ("queue_weight_super_admin", "queue_weight_admin", "queue_weight_user"):
        v = getattr(s, _wf, None)
        if v is not None:
            setattr(s, _wf, max(1, min(20, int(v))))
    # Likewise constrain storage_backend to known values.
    if s.storage_backend not in ("local", "s3"):
        s.storage_backend = "local"

    s.updated_by_user_id = actor.id
    db.commit()
    db.refresh(s)
    # Any storage_*-affecting PATCH invalidates the cached backend so
    # the very next upload picks up the new config. Cheap — the cache
    # is just a single module-level reference. Doing it unconditionally
    # is fine; the next current_backend() call rebuilds from the row.
    try:
        from ..services.storage import invalidate_cache as _inv
        _inv()
    except Exception:
        pass
    # Same idea for the engine's streaming-safety divisor — push the
    # current value down so the Performance slider takes effect for the
    # next conversion without a restart. (max_concurrent_conversions
    # genuinely needs a restart — the ThreadPoolExecutor isn't
    # resizable — and the UI labels that field accordingly.)
    if req.streaming_safety_divisor is not None:
        try:
            from app.core.config import set_safety_divisor as _set_div
            _set_div(int(s.streaming_safety_divisor or 4))
        except Exception:
            pass
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

    import aiosmtplib
    from ..auth.crypto import decrypt
    from ..auth.email import _button_html, _html_email

    relay = f"{s.smtp_host}:{s.smtp_port or 587}"
    auth = s.smtp_user or "(no auth)"
    bn = getattr(s, "brand_title", None) or "VITRIOL"
    plain = (
        f"If you can read this, SMTP is wired up correctly.\n\n"
        f"Sent by {bn} via {relay} as {auth}.\n"
    )
    from ..auth.email import public_url as _pub_url, _email_border as _eborder
    _primary, _hue, _bstyle = _eborder(db)
    html = _html_email(
        title=f"{bn} — SMTP test",
        greeting="SMTP is working!",
        paragraphs=[
            f"This is a test message sent from your {bn} server to verify that outgoing email is configured correctly.",
            f"Relay: {relay}    Auth: {auth}",
        ],
        button_html=_button_html(f"Open {bn}", str(s.public_base_url or "/"), _primary),
        footer="You can safely ignore this email — it was triggered by the SMTP test button in Server settings.",
        brand=bn,
        logo_url=_pub_url(db, "api/v1/server/branding/logo"),
        border_hue=_hue,
        border_style=_bstyle,
        brand_color=_primary,
    )

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("alternative")
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = f"{bn} — SMTP test"
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    from datetime import datetime as _dt
    # Hard 30s budget for the entire SMTP round trip. aiosmtplib's
    # default is 60s for both connection and per-command timeouts;
    # behind a Cloudflare/Coolify proxy chain that's longer than the
    # proxy's own request timeout, so a hung SMTP server (purelymail
    # rate-limit, network glitch, etc.) surfaces as "502 Bad Gateway"
    # with no actionable detail. 30s lets us beat the proxy and
    # return a real error message.
    # STARTTLS checkbox checked → connect plain then upgrade (start_tls)
    # STARTTLS checkbox unchecked → implicit TLS from the start (use_tls)
    if s.smtp_use_tls:
        tls_kwargs = {"use_tls": False, "start_tls": True}
    else:
        tls_kwargs = {"use_tls": True, "start_tls": False}
    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user or None,
            password=decrypt(s.smtp_password_enc) or None,
            **tls_kwargs,
            timeout=30,
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


@router.post("/test-email-preview", response_model=MessageResponse)
async def test_email_preview(
    payload: dict,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Send one of the real email templates as a preview so the super admin
    can see exactly how it will look in their inbox.

    ``kind`` must be one of ``verification``, ``reset``, or ``approval``.
    ``to`` is optional — falls back to the actor's own email address.
    """
    from ..auth.email import _button_html, _html_email, _send, _brand, public_url, _email_border
    from html import escape as _esc

    s = db.query(ServerSettings).get(1)
    if s is None or not s.smtp_host or not s.smtp_from:
        raise HTTPException(status_code=400, detail="SMTP is not configured.")
    kind = str((payload or {}).get("kind", "")).strip()
    to = str((payload or {}).get("to", "")).strip() or actor.email
    if "@" not in to:
        raise HTTPException(status_code=400, detail="Recipient address looks invalid.")
    if kind not in ("verification", "reset", "approval"):
        raise HTTPException(status_code=400, detail="kind must be verification, reset, or approval.")

    bn = _brand(db)
    logo = public_url(db, "api/v1/server/branding/logo")
    primary, hue, bstyle = _email_border(db)
    preview_link = public_url(db, {
        "verification": "verify?token=PREVIEW_NOT_VALID",
        "reset": "reset?token=PREVIEW_NOT_VALID",
        "approval": "admin/users",
    }[kind])

    if kind == "verification":
        subject = f"[Preview] Verify your {bn} account"
        plain = (
            f"Hello {actor.username},\n\n"
            f"Verify your {bn} account by visiting:\n{preview_link}\n\n"
            "This link expires in 24 hours."
        )
        html = _html_email(
            title=subject,
            greeting=f"Hello, {actor.username}!",
            paragraphs=[f"Click the button below to verify your {bn} account."],
            button_html=_button_html("Verify my account", preview_link, primary),
            footer="This link expires in 24 hours. If you didn't create an account, you can safely ignore this email.",
            brand=bn,
            logo_url=logo,
            border_hue=hue,
            border_style=bstyle,
            brand_color=primary,
        )
    elif kind == "reset":
        subject = f"[Preview] Reset your {bn} password"
        plain = (
            f"Hello {actor.username},\n\n"
            f"Reset your {bn} password by visiting:\n{preview_link}\n\n"
            "This link expires in 2 hours. If you didn't request this, ignore this email."
        )
        html = _html_email(
            title=subject,
            greeting=f"Hello, {actor.username}!",
            paragraphs=[f"We received a request to reset your {bn} password. Click the button below to choose a new one."],
            button_html=_button_html("Reset my password", preview_link, primary),
            footer=f"This link expires in 2 hours. If you didn't request a password reset, you can safely ignore this email — your password has not been changed.",
            brand=bn,
            logo_url=logo,
            border_hue=hue,
            border_style=bstyle,
            brand_color=primary,
        )
    else:  # approval
        subject = f"[Preview] {bn} — new user awaiting approval"
        plain = (
            f"A new user has signed up on {bn} and is awaiting approval.\n\n"
            f"Username: {actor.username}\n"
            f"Email: {actor.email}\n\n"
            f"Approve or deny here: {preview_link}\n"
        )
        detail_rows = (
            f'<tr><td style="font-family:Arial,sans-serif;font-size:14px;color:#555555;padding:4px 0;width:90px;">Username</td>'
            f'<td style="font-family:Arial,sans-serif;font-size:14px;color:#111111;padding:4px 0;">{_esc(actor.username)}</td></tr>'
            f'<tr><td style="font-family:Arial,sans-serif;font-size:14px;color:#555555;padding:4px 0;">Email</td>'
            f'<td style="font-family:Arial,sans-serif;font-size:14px;color:#111111;padding:4px 0;">{_esc(actor.email)}</td></tr>'
        )
        details_table = (
            f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
            f'style="margin:0 0 24px 0;border-left:3px solid {primary};padding-left:12px;">'
            f'{detail_rows}</table>'
        )
        html = _html_email(
            title=subject,
            greeting="New user awaiting approval",
            paragraphs=[f"A new account has been created on {bn} and is waiting for your review."],
            button_html=details_table + _button_html("Review in admin panel", preview_link, primary),
            brand=bn,
            logo_url=logo,
            border_hue=hue,
            border_style=bstyle,
            brand_color=primary,
        )

    ok = await _send(db, to, subject, plain, html)
    if not ok:
        raise HTTPException(status_code=502, detail="SMTP delivery failed — check SMTP settings.")
    audit.log(db, actor.id, "smtp_preview_test", metadata={"kind": kind, "to": to})
    return MessageResponse(message=f"Preview '{kind}' email sent to {to}.")


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


@router.post("/test-s3", response_model=MessageResponse)
def test_s3(
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """HEAD the configured S3 bucket as a connectivity + permissions
    smoke test. Stamps ``s3_last_test_*`` so the UI badge sticks across
    reloads (mirrors the SMTP / Discord / OIDC test pattern)."""
    from datetime import datetime as _dt
    s = db.query(ServerSettings).get(1)
    if s is None:
        raise HTTPException(status_code=500, detail="Settings row missing")
    if not (s.s3_bucket and s.s3_access_key and s.s3_secret_key_enc):
        raise HTTPException(
            status_code=400,
            detail="S3 config incomplete — bucket, access key, and secret key are all required.",
        )
    from ..services.storage import S3Backend
    err: str | None = None
    try:
        backend = S3Backend(
            endpoint_url=s.s3_endpoint_url,
            bucket=s.s3_bucket,
            region=s.s3_region,
            access_key=s.s3_access_key,
            secret_key=decrypt(s.s3_secret_key_enc) or "",
            path_prefix=s.s3_path_prefix,
            force_path_style=bool(s.s3_force_path_style),
        )
        backend.head_bucket()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    s.s3_last_test_at = _dt.utcnow()
    s.s3_last_test_ok = err is None
    db.commit()
    audit.log(db, actor.id, "s3_test", metadata={"ok": s.s3_last_test_ok, "error": err})
    if err is not None:
        raise HTTPException(status_code=502, detail=f"S3 test failed: {err}")
    # Cache invalidation so the next storage operation picks up the
    # freshly-saved config without a restart.
    from ..services.storage import invalidate_cache as _inv
    _inv()
    return MessageResponse(message="S3 connection ok.")


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


# --------------------------------------------------------- branding logo
#
# Operator-uploaded nav logo. Lives under `{data_dir}/branding/` so it
# survives container restarts on the persistent volume (same place the
# SQLite DB and cert chain live). The filename — not the full path —
# is stored on ServerSettings so a /data path rename doesn't invalidate
# the row. When no upload exists, the GET endpoint redirects to the
# bundled /static/img/logo.svg.

# Lightweight MIME → extension whitelist. Anything outside this set is
# refused at upload time. Extensions are normalized so the served
# Content-Type is honest about what the file actually is.
_ALLOWED_LOGO_TYPES = {
    "image/svg+xml":  ".svg",
    "image/png":      ".png",
    "image/jpeg":     ".jpg",
    "image/webp":     ".webp",
    "image/gif":      ".gif",
}

_LOGO_MAX_BYTES = 1 * 1024 * 1024   # 1 MB — logos shouldn't be huge


def _branding_dir() -> Path:
    p = _cfg.data_dir / "branding"
    p.mkdir(parents=True, exist_ok=True)
    return p


@router.get("/branding/logo")
def get_branding_logo(db: Session = Depends(get_db)):
    """Public — every authed page renders the nav logo via this URL.

    Resolves in priority order:
      1. The operator-uploaded file at ``{data_dir}/branding/{filename}``
         if both the column and the on-disk file exist.
      2. Redirect to the bundled /static/img/logo.svg fallback.

    Cache-Control allows short browser caching but lets a logo upload
    propagate within ~60 s; an aggressive cache here would mean a logo
    change takes a hard refresh to show up.
    """
    s = db.query(ServerSettings).get(1)
    filename = getattr(s, "brand_logo_filename", None) if s else None
    if filename:
        target = _branding_dir() / filename
        if target.exists():
            # Honest Content-Type from the extension; FileResponse
            # also handles ETag + Content-Length automatically.
            ext = target.suffix.lower()
            media_type = next(
                (mt for mt, e in _ALLOWED_LOGO_TYPES.items() if e == ext),
                "application/octet-stream",
            )
            resp = FileResponse(target, media_type=media_type)
            resp.headers["Cache-Control"] = "public, max-age=60"
            return resp
    # Fallback — bundled default. 302 so the browser caches it under
    # the static URL (which has long-lived caching) on next request.
    return RedirectResponse(url="/static/img/logo.svg", status_code=302)


@router.post("/branding/logo", response_model=ServerSettingsOut)
async def upload_branding_logo(
    file: UploadFile = File(...),
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Super-admin only. Accepts a single image file, validates type +
    size, stores under ``{data_dir}/branding/logo.<ext>`` (overwriting
    any prior upload), and points the ServerSettings row at it."""
    if file.content_type not in _ALLOWED_LOGO_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported logo type {file.content_type!r}. "
                f"Allowed: {sorted(_ALLOWED_LOGO_TYPES.keys())}."
            ),
        )
    ext = _ALLOWED_LOGO_TYPES[file.content_type]
    # Read into memory — logos are bounded by _LOGO_MAX_BYTES, so this
    # is fine. Streaming would just complicate size enforcement.
    raw = await file.read()
    if len(raw) > _LOGO_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Logo exceeds max size ({_LOGO_MAX_BYTES:,} bytes).",
        )
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Logo file is empty.")
    # Light SVG scrub — strip <script> and on*= attribute prefixes.
    # The nav renders the logo via <img src>, where browsers don't
    # execute SVG-embedded JS anyway, but this scrub is cheap and
    # protects against any future caller that inlines the SVG.
    if ext == ".svg":
        try:
            text = raw.decode("utf-8", errors="replace")
            import re
            text = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'\son[a-z]+\s*=\s*"[^"]*"', "", text, flags=re.IGNORECASE)
            text = re.sub(r"\son[a-z]+\s*=\s*'[^']*'", "", text, flags=re.IGNORECASE)
            raw = text.encode("utf-8")
        except Exception:
            # If decode fails it's not a real SVG — bail.
            raise HTTPException(status_code=400, detail="SVG file could not be parsed.")

    # Delete any previous upload (might have a different extension) so
    # the directory doesn't accumulate orphans.
    s = db.query(ServerSettings).get(1)
    prior = getattr(s, "brand_logo_filename", None) if s else None
    if prior:
        try:
            (_branding_dir() / prior).unlink(missing_ok=True)
        except OSError:
            pass

    target_name = f"logo{ext}"
    (_branding_dir() / target_name).write_bytes(raw)
    if s is not None:
        s.brand_logo_filename = target_name
        s.updated_by_user_id = actor.id
        db.commit()
        db.refresh(s)
    audit.log(db, actor.id, "branding_logo_upload",
              metadata={"filename": target_name, "bytes": len(raw), "content_type": file.content_type})
    return _to_out(s)


@router.delete("/branding/logo", response_model=ServerSettingsOut)
def delete_branding_logo(
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Revert to the bundled default logo. Deletes the operator
    upload from disk and clears the filename column."""
    s = db.query(ServerSettings).get(1)
    filename = getattr(s, "brand_logo_filename", None) if s else None
    if filename:
        try:
            (_branding_dir() / filename).unlink(missing_ok=True)
        except OSError:
            pass
    if s is not None:
        s.brand_logo_filename = None
        s.updated_by_user_id = actor.id
        db.commit()
        db.refresh(s)
    audit.log(db, actor.id, "branding_logo_delete")
    return _to_out(s)
