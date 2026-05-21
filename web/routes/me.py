"""Self-service: profile, password, API keys, request access (Viewer→User)."""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import api_keys as api_keys_svc
from ..auth import email as email_svc
from ..auth.password import hash_password, verify_password
from ..auth import discord as discord_svc
from ..deps import get_current_user, get_db
from ..models import APIKey, ApprovalRequest, ApprovalStatus, Role, ServerSettings, TokenPurpose, User
from ..schemas import (
    APIKeyCreateRequest, APIKeyCreateResponse, APIKeyOut, MessageResponse,
    PasswordChangeRequest, SelfUpdateRequest, SupportRequest, UserOut,
)
from ..services import audit

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.delete("", response_model=MessageResponse)
def delete_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Allow a user to permanently delete their own account.

    Gated by two toggles:
      1. ServerSettings.allow_self_delete (master switch — default on).
      2. CustomRole.can_self_delete (per-role, only checked when the user
         has a custom role — default on).
    Super admins can never self-delete.
    """
    if user.role == Role.super_admin:
        raise HTTPException(status_code=403, detail="The super admin account cannot be self-deleted.")
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None or not bool(getattr(s, "allow_self_delete", True)):
        raise HTTPException(status_code=403, detail="Account self-deletion is disabled on this server.")
    cr = getattr(user, "custom_role", None)
    if cr is not None and not bool(getattr(cr, "can_self_delete", True)):
        raise HTTPException(status_code=403, detail="Your role does not permit account self-deletion.")
    uid = user.id
    db.delete(user)
    db.commit()
    audit.log(db, None, "self_delete", target_user_id=uid)
    return MessageResponse(message="Account deleted.")


VALID_THEMES = {"default", "crimson", "verdant", "cobalt", "parchment", "obsidian"}
VALID_BORDER_STYLES = {"", "vitriol", "runes", "arcane", "circuit", "minimal", "vine", "helix"}


@router.patch("", response_model=UserOut)
async def update_me(req: SelfUpdateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if req.username and req.username != user.username:
        if db.query(User).filter(User.username == req.username).count():
            raise HTTPException(status_code=409, detail="Username taken")
        user.username = req.username
    if req.email and req.email != user.email:
        if db.query(User).filter(User.email == req.email).count():
            raise HTTPException(status_code=409, detail="Email already registered")
        # Send a verification mail to the new address before applying.
        raw = email_svc.issue_verification_token(db, user, TokenPurpose.change_email, new_email=req.email)
        await email_svc.send_verification_email(db, user, raw)
    if req.theme is not None:
        if req.theme not in VALID_THEMES:
            raise HTTPException(status_code=400, detail=f"Unknown theme. Choose from {sorted(VALID_THEMES)}")
        user.theme = req.theme
    if req.show_border is not None:
        user.show_border = bool(req.show_border)
    if req.border_style is not None:
        style = req.border_style or ""
        if style not in VALID_BORDER_STYLES:
            raise HTTPException(status_code=400, detail=f"Unknown border style. Choose from {sorted(s for s in VALID_BORDER_STYLES if s)}")
        user.border_style = style or None  # empty string → clear override
    if req.first_name is not None:
        user.first_name = req.first_name.strip() or None
    if req.last_name is not None:
        user.last_name = req.last_name.strip() or None
    db.commit()
    db.refresh(user)
    audit.log(db, user.id, "self_update", target_user_id=user.id)
    return user


@router.post("/password", response_model=MessageResponse)
def change_password(req: PasswordChangeRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.password_hash:
        if not req.current_password or not verify_password(req.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Current password incorrect")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    audit.log(db, user.id, "self_password_change", target_user_id=user.id)
    return MessageResponse(message="Password updated.")


# ------------------------------------------------------------- API keys

@router.get("/api-keys", response_model=List[APIKeyOut])
def list_api_keys(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return (
        db.query(APIKey)
        .filter(APIKey.user_id == user.id)
        .order_by(APIKey.created_at.desc())
        .all()
    )


@router.post("/api-keys", response_model=APIKeyCreateResponse)
def create_api_key(req: APIKeyCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    full, prefix, h = api_keys_svc.issue()
    row = APIKey(user_id=user.id, name=req.name, key_hash=h, prefix=prefix)
    db.add(row)
    db.commit()
    db.refresh(row)
    audit.log(db, user.id, "api_key_create", target_user_id=user.id, metadata={"prefix": prefix})
    return APIKeyCreateResponse(
        id=row.id, name=row.name, prefix=row.prefix,
        created_at=row.created_at, last_used_at=row.last_used_at, revoked_at=row.revoked_at,
        secret=full,
    )


@router.delete("/api-keys/{key_id}", response_model=MessageResponse)
def revoke_api_key(key_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row: Optional[APIKey] = db.query(APIKey).filter(APIKey.id == key_id, APIKey.user_id == user.id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found")
    row.revoked_at = datetime.utcnow()
    db.commit()
    audit.log(db, user.id, "api_key_revoke", target_user_id=user.id, metadata={"prefix": row.prefix})
    return MessageResponse(message="API key revoked.")


# ---------------------------------------------------------- Viewer ask

@router.post("/request-access", response_model=MessageResponse)
async def request_access(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != Role.viewer:
        raise HTTPException(status_code=400, detail="Only viewers can request access")
    existing = (
        db.query(ApprovalRequest)
        .filter(ApprovalRequest.user_id == user.id, ApprovalRequest.status == ApprovalStatus.pending)
        .one_or_none()
    )
    if existing is None:
        ar = ApprovalRequest(user_id=user.id, requested_role=Role.user)
        db.add(ar)
        db.commit()
    admins = (
        db.query(User)
        .filter(User.role.in_([Role.super_admin, Role.admin]))
        .filter(User.email.isnot(None))
        .all()
    )
    await email_svc.send_pending_approval_notification(db, user, [a.email for a in admins if a.email])
    from ..services.notifications import notify_all
    await notify_all(db, f":eye: Viewer **{user.username}** requested access upgrade.")
    audit.log(db, user.id, "request_access", target_user_id=user.id)
    return MessageResponse(message="Request submitted.")


@router.post("/support-request", response_model=MessageResponse)
async def support_request(
    req: SupportRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = db.query(ServerSettings).get(1)
    if not s or not getattr(s, "show_user_support_button", False):
        raise HTTPException(status_code=403, detail="Support requests are not enabled.")
    support_email = getattr(s, "support_email", None)
    if not support_email:
        raise HTTPException(status_code=503, detail="No support email configured.")

    from html import escape
    subject = (req.subject or "Support Request").strip()
    display_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username
    plain = (
        f"Support request from {display_name} ({user.username})\n"
        f"Email: {user.email or 'not provided'}\n\n"
        f"Subject: {subject}\n\n"
        f"{req.message}"
    )
    html_body = (
        f"<p><strong>From:</strong> {escape(display_name)} "
        f"(<code>{escape(user.username)}</code>)</p>"
        f"<p><strong>Email:</strong> {escape(user.email or 'not provided')}</p>"
        f"<p><strong>Subject:</strong> {escape(subject)}</p>"
        f"<hr/><p style='white-space:pre-wrap'>{escape(req.message)}</p>"
    )
    await email_svc._send(db, support_email, f"[Support] {subject}", plain, html_body)
    audit.log(db, user.id, "support_request", target_user_id=user.id)
    return MessageResponse(message="Your message has been sent.")
