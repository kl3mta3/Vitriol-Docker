"""SMTP email sending. Settings are pulled from the server_settings row.

Silently no-ops when SMTP isn't configured (logs a warning) so dev runs
don't crash on signup.
"""
from __future__ import annotations
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Optional

from sqlalchemy.orm import Session

from ..models import EmailVerificationToken, PasswordResetToken, ServerSettings, TokenPurpose, User
from .crypto import decrypt

logger = logging.getLogger("vitriol.email")

_BRAND_COLOR = "#8b5cf6"
_BRAND_COLOR_DARK = "#7c3aed"


def _settings_row(db: Session) -> Optional[ServerSettings]:
    return db.query(ServerSettings).get(1)


def _button_html(label: str, url: str) -> str:
    """Inline-CSS CTA button block compatible with most email clients."""
    safe_url = escape(url, quote=True)
    safe_label = escape(label)
    return f"""\
<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:24px 0;">
  <tr>
    <td style="border-radius:6px;background:{_BRAND_COLOR};">
      <a href="{safe_url}" target="_blank"
         style="display:inline-block;padding:12px 28px;font-family:Arial,sans-serif;
                font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;
                border-radius:6px;background:{_BRAND_COLOR};">{safe_label}</a>
    </td>
  </tr>
</table>
<p style="margin:0 0 4px 0;font-family:Arial,sans-serif;font-size:12px;color:#888888;">
  Button not working? Copy and paste this link into your browser:
</p>
<p style="margin:0 0 24px 0;word-break:break-all;">
  <a href="{safe_url}" style="font-family:monospace;font-size:12px;color:{_BRAND_COLOR};">{escape(url)}</a>
</p>"""


def _html_email(title: str, greeting: str, paragraphs: list[str], button_html: str, footer: str = "") -> str:
    """Full HTML email shell with inline CSS."""
    safe_title = escape(title)
    para_html = "".join(
        f'<p style="margin:0 0 16px 0;font-family:Arial,sans-serif;font-size:15px;'
        f'line-height:1.6;color:#333333;">{escape(p)}</p>'
        for p in paragraphs
    )
    footer_html = (
        f'<p style="margin:32px 0 0 0;font-family:Arial,sans-serif;font-size:12px;'
        f'color:#888888;border-top:1px solid #eeeeee;padding-top:16px;">{escape(footer)}</p>'
        if footer else ""
    )
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{safe_title}</title></head>
<body style="margin:0;padding:0;background:#f5f5f5;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0"
       width="100%" style="background:#f5f5f5;">
  <tr><td style="padding:40px 20px;">
    <table role="presentation" cellspacing="0" cellpadding="0" border="0"
           width="600" style="max-width:600px;margin:0 auto;background:#ffffff;
                              border-radius:8px;overflow:hidden;
                              box-shadow:0 2px 8px rgba(0,0,0,0.08);">
      <!-- header bar -->
      <tr><td style="background:{_BRAND_COLOR};padding:24px 32px;">
        <span style="font-family:Arial,sans-serif;font-size:22px;font-weight:700;
                     color:#ffffff;letter-spacing:0.05em;">VITRIOL</span>
      </td></tr>
      <!-- body -->
      <tr><td style="padding:32px 32px 24px 32px;">
        <p style="margin:0 0 24px 0;font-family:Arial,sans-serif;font-size:17px;
                  font-weight:600;color:#111111;">{escape(greeting)}</p>
        {para_html}
        {button_html}
        {footer_html}
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


async def _send(db: Session, to: str, subject: str, plain: str, html: Optional[str] = None) -> bool:
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

    if html:
        msg = MIMEMultipart("alternative")
        msg["From"] = s.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = s.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(plain)

    try:
        import aiosmtplib
        password = decrypt(s.smtp_password_enc)
        # STARTTLS checkbox checked → connect plain then upgrade (start_tls)
        # STARTTLS checkbox unchecked → implicit TLS from the start (use_tls)
        if s.smtp_use_tls:
            tls_kwargs = {"use_tls": False, "start_tls": True}
        else:
            tls_kwargs = {"use_tls": True, "start_tls": False}
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user or None,
            password=password or None,
            **tls_kwargs,
            timeout=30,
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
    plain = (
        f"Hello {user.username},\n\n"
        f"Verify your Vitriol account by visiting:\n{link}\n\n"
        "This link expires in 24 hours."
    )
    html = _html_email(
        title="Verify your Vitriol account",
        greeting=f"Hello, {user.username}!",
        paragraphs=["Click the button below to verify your Vitriol account."],
        button_html=_button_html("Verify my account", link),
        footer="This link expires in 24 hours. If you didn't create an account, you can safely ignore this email.",
    )
    return await _send(db, user.email, "Verify your Vitriol account", plain, html)


async def send_password_reset_email(db: Session, user: User, raw_token: str) -> bool:
    link = public_url(db, f"reset?token={raw_token}")
    plain = (
        f"Hello {user.username},\n\n"
        f"Reset your Vitriol password by visiting:\n{link}\n\n"
        "This link expires in 2 hours. If you didn't request this, ignore this email."
    )
    html = _html_email(
        title="Reset your Vitriol password",
        greeting=f"Hello, {user.username}!",
        paragraphs=["We received a request to reset your Vitriol password. Click the button below to choose a new one."],
        button_html=_button_html("Reset my password", link),
        footer="This link expires in 2 hours. If you didn't request a password reset, you can safely ignore this email — your password has not been changed.",
    )
    return await _send(db, user.email, "Reset your Vitriol password", plain, html)


async def send_pending_approval_notification(db: Session, pending_user: User, recipients: list[str]) -> int:
    sent = 0
    admin_url = public_url(db, "admin/users")
    name_parts = [pending_user.first_name or "", pending_user.last_name or ""]
    display_name = " ".join(p for p in name_parts if p).strip() or None
    name_line = f"Name: {display_name}\n" if display_name else ""
    plain = (
        f"A new user has signed up and is awaiting approval.\n\n"
        f"Username: {pending_user.username}\n"
        f"{name_line}"
        f"Email: {pending_user.email}\n\n"
        f"Approve or deny here: {admin_url}\n"
    )
    detail_rows = (
        f'<tr><td style="font-family:Arial,sans-serif;font-size:14px;color:#555555;'
        f'padding:4px 0;width:90px;">Username</td>'
        f'<td style="font-family:Arial,sans-serif;font-size:14px;color:#111111;padding:4px 0;">'
        f'{escape(pending_user.username)}</td></tr>'
    )
    if display_name:
        detail_rows += (
            f'<tr><td style="font-family:Arial,sans-serif;font-size:14px;color:#555555;'
            f'padding:4px 0;">Name</td>'
            f'<td style="font-family:Arial,sans-serif;font-size:14px;color:#111111;padding:4px 0;">'
            f'{escape(display_name)}</td></tr>'
        )
    detail_rows += (
        f'<tr><td style="font-family:Arial,sans-serif;font-size:14px;color:#555555;'
        f'padding:4px 0;">Email</td>'
        f'<td style="font-family:Arial,sans-serif;font-size:14px;color:#111111;padding:4px 0;">'
        f'{escape(pending_user.email)}</td></tr>'
    )
    details_table = (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        f'style="margin:0 0 24px 0;border-left:3px solid {_BRAND_COLOR};padding-left:12px;">'
        f'{detail_rows}</table>'
    )
    html = _html_email(
        title="Vitriol — new user awaiting approval",
        greeting="New user awaiting approval",
        paragraphs=["A new account has been created and is waiting for your review."],
        button_html=details_table + _button_html("Review in admin panel", admin_url),
    )
    for to in recipients:
        if not to:
            continue
        if await _send(db, to, "Vitriol — new user awaiting approval", plain, html):
            sent += 1
    return sent
