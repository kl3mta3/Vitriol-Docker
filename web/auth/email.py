"""SMTP email sending. Settings are pulled from the server_settings row.

Silently no-ops when SMTP isn't configured (logs a warning) so dev runs
don't crash on signup.
"""
from __future__ import annotations
import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional

from sqlalchemy.orm import Session

from ..models import EmailVerificationToken, PasswordResetToken, ServerSettings, TokenPurpose, User
from .crypto import decrypt

logger = logging.getLogger("vitriol.email")


def _settings_row(db: Session) -> Optional[ServerSettings]:
    return db.query(ServerSettings).get(1)


async def _send(db: Session, to: str, subject: str, body: str) -> bool:
    s = _settings_row(db)
    if s is None or not s.smtp_host or not s.smtp_from:
        logger.warning(
            "SMTP not configured — skipping email to %s (subject=%r). "
            "Host set=%s, From set=%s.",
            to, subject, bool(s and s.smtp_host), bool(s and s.smtp_from),
        )
        return False
    # Respect the operator's master enable toggle. Credentials may be
    # saved but the integration is paused — no outbound mail.
    if not bool(s.smtp_enabled):
        logger.info(
            "SMTP disabled by master toggle — skipping email to %s (subject=%r).",
            to, subject,
        )
        return False
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        import aiosmtplib
        password = decrypt(s.smtp_password_enc)
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port or 587,
            username=s.smtp_user or None,
            password=password or None,
            start_tls=bool(s.smtp_use_tls),
        )
        return True
    except Exception as e:
        # Log the full exception so operators can see *why* a real signup
        # email is failing instead of getting a silent "no email arrived".
        logger.exception(
            "SMTP send failed for %s (subject=%r) via %s:%s as %s — %s",
            to, subject, s.smtp_host, s.smtp_port or 587,
            s.smtp_user or "(no auth)", e,
        )
        return False


def issue_verification_token(db: Session, user: User, purpose: TokenPurpose, new_email: Optional[str] = None) -> str:
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    row = EmailVerificationToken(
        token_hash=h, user_id=user.id, purpose=purpose,
        new_email=new_email,
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.add(row)
    db.commit()
    return raw


def consume_verification_token(db: Session, raw: str) -> Optional[EmailVerificationToken]:
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    row = db.query(EmailVerificationToken).filter(EmailVerificationToken.token_hash == h).one_or_none()
    if row is None:
        return None
    if row.used_at is not None or row.expires_at < datetime.utcnow():
        return None
    row.used_at = datetime.utcnow()
    db.commit()
    return row


def issue_password_reset_token(db: Session, user: User) -> str:
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    row = PasswordResetToken(
        token_hash=h, user_id=user.id,
        expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    db.add(row)
    db.commit()
    return raw


def consume_password_reset_token(db: Session, raw: str) -> Optional[PasswordResetToken]:
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    row = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == h).one_or_none()
    if row is None or row.used_at is not None or row.expires_at < datetime.utcnow():
        return None
    row.used_at = datetime.utcnow()
    db.commit()
    return row


def public_url(db: Session, path: str) -> str:
    s = _settings_row(db)
    base = (s.public_base_url if s and s.public_base_url else "").strip()
    if not base:
        # No public URL configured — emails would otherwise ship a
        # `localhost:3825/...` link that nobody outside the container can
        # reach. Log loudly so the operator notices the misconfiguration
        # the first time a verification email goes out.
        logger.warning(
            "public_base_url is empty — verification/reset links will use "
            "http://localhost:3825 as the fallback host. Set Public base URL "
            "under /admin/server."
        )
        base = "http://localhost:3825"
    return base.rstrip("/") + "/" + path.lstrip("/")


async def send_verification_email(db: Session, user: User, raw_token: str) -> bool:
    # Points at the public-facing /verify HTML page (handled by ui.py)
    # rather than the bare /api/v1/auth/verify JSON endpoint, so a
    # browser click renders a real success page instead of `{"message":...}`.
    link = public_url(db, f"verify?token={raw_token}")
    body = (
        f"Hello {user.username},\n\n"
        f"Verify your Vitriol account by visiting:\n{link}\n\n"
        "This link expires in 24 hours."
    )
    return await _send(db, user.email, "Verify your Vitriol account", body)


async def send_password_reset_email(db: Session, user: User, raw_token: str) -> bool:
    link = public_url(db, f"reset?token={raw_token}")
    body = (
        f"Hello {user.username},\n\n"
        f"Reset your Vitriol password by visiting:\n{link}\n\n"
        "This link expires in 2 hours. If you didn't request this, ignore this email."
    )
    return await _send(db, user.email, "Reset your Vitriol password", body)


async def send_pending_approval_notification(db: Session, pending_user: User, recipients: list[str]) -> int:
    sent = 0
    base = public_url(db, "admin/users")
    body = (
        f"A new user has signed up and is awaiting approval.\n\n"
        f"Username: {pending_user.username}\n"
        f"Email: {pending_user.email}\n\n"
        f"Approve or deny here: {base}\n"
    )
    for to in recipients:
        if not to:
            continue
        if await _send(db, to, "Vitriol — new user awaiting approval", body):
            sent += 1
    return sent
