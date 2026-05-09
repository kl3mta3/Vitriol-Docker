"""FastAPI dependencies: DB session, current user, role guards."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from .auth.api_keys import find_active, looks_like_api_key
from .auth.jwt import decode_access_token
from .auth.permissions import has_capability, is_super_admin, is_admin_or_above
from .db import SessionLocal
from .models import Role, Status, User


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _expire_suspension_if_due(db: Session, user: User) -> None:
    if user.status == Status.suspended and user.suspended_until and user.suspended_until <= datetime.utcnow():
        user.status = Status.active
        user.suspended_until = None
        user.suspension_reason = None
        db.commit()


def _user_from_bearer(db: Session, token: str) -> Optional[User]:
    if looks_like_api_key(token):
        row = find_active(db, token)
        if row is None:
            return None
        row.last_used_at = datetime.utcnow()
        db.commit()
        return db.query(User).get(row.user_id)
    payload = decode_access_token(token)
    if payload is None or payload.get("typ") != "access":
        return None
    try:
        uid = int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        return None
    return db.query(User).get(uid)


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
    vitriol_access: Optional[str] = Cookie(None),
) -> Optional[User]:
    token: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif vitriol_access:
        token = vitriol_access
    if not token:
        return None
    user = _user_from_bearer(db, token)
    if user is None:
        return None
    _expire_suspension_if_due(db, user)
    if user.status == Status.banned:
        return None
    return user


def get_current_user(user: Optional[User] = Depends(get_current_user_optional)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_capability(cap: str):
    def _dep(user: User = Depends(get_current_user)) -> User:
        if not has_capability(user, cap):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing capability: {cap}")
        return user
    return _dep


def require_super_admin(user: User = Depends(get_current_user)) -> User:
    if not is_super_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin only")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_admin_or_above(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def require_active_non_pending(user: User = Depends(get_current_user)) -> User:
    if user.role == Role.pending:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account pending approval")
    if user.status != Status.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Account is {user.status.value}")
    return user
