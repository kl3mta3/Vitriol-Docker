"""HTML page routes (Jinja2 templates)."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth.permissions import has_capability, role_capabilities, CAN_VIEW_USERS_TAB, CAN_VIEW_SERVER_TAB
from ..deps import get_current_user_optional, get_db
from ..models import Role, ServerSettings, User

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


def _common_ctx(request: Request, user: Optional[User], db: Session) -> dict:
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    caps: list[str] = []
    if user is not None:
        caps = sorted(role_capabilities(user.role) | (
            {"use_stone"} if user.stone_enabled else set()
        ) | (
            {"use_self_compile"} if user.self_compile_enabled else set()
        ))
    return {
        "request": request,
        "user": user,
        "caps": caps,
        "allow_signup": bool(s.allow_signup) if s else False,
        "show_users_tab": user is not None and has_capability(user, CAN_VIEW_USERS_TAB),
        "show_server_tab": user is not None and has_capability(user, CAN_VIEW_SERVER_TAB),
    }


@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    if user is None:
        return RedirectResponse(url="/signin")
    if user.role == Role.pending:
        return templates.TemplateResponse(request, "pending.html", _common_ctx(request, user, db))
    return templates.TemplateResponse(request, "app.html", _common_ctx(request, user, db))


@router.get("/signin", response_class=HTMLResponse)
def signin_page(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    if user is not None:
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "signin.html", _common_ctx(request, None, db))


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    if user is not None:
        return RedirectResponse(url="/")
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None or not s.allow_signup:
        return RedirectResponse(url="/signin")
    return templates.TemplateResponse(request, "signup.html", _common_ctx(request, None, db))


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    if user is None:
        return RedirectResponse(url="/signin")
    return templates.TemplateResponse(request, "profile.html", _common_ctx(request, user, db))


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    if user is None:
        return RedirectResponse(url="/signin")
    if not has_capability(user, CAN_VIEW_USERS_TAB):
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "admin_users.html", _common_ctx(request, user, db))


@router.get("/admin/server", response_class=HTMLResponse)
def admin_server_page(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    if user is None:
        return RedirectResponse(url="/signin")
    if not has_capability(user, CAN_VIEW_SERVER_TAB):
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "admin_server.html", _common_ctx(request, user, db))


@router.get("/admin/server/guide", response_class=HTMLResponse)
def admin_server_guide_page(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    """Super-admin-only setup guide. Lives under /admin/server so the same
    capability check (`view_server_tab`) gates access — no separate role."""
    if user is None:
        return RedirectResponse(url="/signin")
    if not has_capability(user, CAN_VIEW_SERVER_TAB):
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "admin_server_guide.html", _common_ctx(request, user, db))
