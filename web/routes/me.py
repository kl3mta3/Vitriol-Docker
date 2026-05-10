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
from ..models import APIKey, ApprovalRequest, ApprovalStatus, Role, TokenPurpose, User
from ..schemas import (
    APIKeyCreateRequest, APIKeyCreateResponse, APIKeyOut, MessageResponse,
    PasswordChangeRequest, SelfUpdateRequest, UserOut,
)
from ..services import audit

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


VALID_THEMES = {"default", "crimson", "verdant", "cobalt", "parchment", "obsidian"}


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
