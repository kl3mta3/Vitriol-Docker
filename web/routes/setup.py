"""First-run setup — only active until a super admin exists.

Flow: a clean install has no super admin row, the app routes everything
to /setup, the operator fills in the form, we create the row and sign
them in. From that moment the route returns 404 forever (or until the
super admin row is wiped — which is also our recovery path).
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from ..auth.jwt import issue_access_token, new_refresh_token, hash_refresh
from ..auth.password import hash_password
from ..config import get_settings
from ..db import SessionLocal
from ..deps import get_db
from ..models import Role, Session_ as SessionRow, Status, User
from ..schemas import TokenResponse
from ..services import audit
from ..services.bootstrap import super_admin_exists

router = APIRouter()
api_router = APIRouter(prefix="/auth", tags=["setup"])
templates = Jinja2Templates(directory="web/templates")
_settings = get_settings()


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: Optional[EmailStr] = None
    password: str = Field(min_length=8, max_length=200)


def setup_required(db: Session) -> bool:
    return not super_admin_exists(db)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    if not setup_required(db):
        # Nothing to set up — kick the visitor to the normal flow.
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/")
    ctx = {
        "request": request,
        "user": None,
        "caps": [],
        "allow_signup": False,
        "show_users_tab": False,
        "show_server_tab": False,
    }
    return templates.TemplateResponse(request, "setup.html", ctx)


@api_router.post("/setup", response_model=TokenResponse)
def setup_create(req: SetupRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    # Idempotent guard — if anyone (curl, automation) tries this after
    # bootstrap, they get a 409 instead of a duplicate.
    if super_admin_exists(db):
        raise HTTPException(status_code=409, detail="Super admin already exists. /setup is closed.")
    user = User(
        username=req.username,
        email=req.email,
        password_hash=hash_password(req.password),
        role=Role.super_admin,
        status=Status.active,
        stone_enabled=True,
        self_compile_enabled=True,
        # The operator clearly owns this address — mark verified so they
        # don't have to chase a verification email through SMTP they
        # haven't even configured yet.
        email_verified_at=datetime.utcnow() if req.email else None,
        last_login_at=datetime.utcnow(),
    )
    db.add(user)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Most likely: someone hit the partial unique index between our
        # super_admin_exists() check and this commit.
        raise HTTPException(status_code=409, detail="Setup raced. Refresh and sign in.")
    db.refresh(user)

    access, expires_in = issue_access_token(user.id, user.role.value)
    raw_refresh, _, _ = new_refresh_token()
    sess = SessionRow(
        user_id=user.id,
        refresh_token_hash=hash_refresh(raw_refresh),
        user_agent=(request.headers.get("user-agent") or "")[:255],
        ip=(request.client.host if request.client else None),
        expires_at=datetime.utcnow() + timedelta(days=_settings.refresh_token_days),
    )
    db.add(sess)
    db.commit()

    response.set_cookie(
        "vitriol_access", access,
        httponly=True, samesite="lax", secure=False,
        max_age=_settings.access_token_minutes * 60, path="/",
    )
    response.set_cookie(
        "vitriol_refresh", raw_refresh,
        httponly=True, samesite="lax", secure=False,
        max_age=_settings.refresh_token_days * 86400, path="/auth",
    )
    audit.log(db, user.id, "setup_create_super_admin", target_user_id=user.id)
    return TokenResponse(access_token=access, expires_in=expires_in)
