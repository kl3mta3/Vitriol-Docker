"""HTML page routes (Jinja2 templates)."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import __version__
from ..auth import permissions as _perms
from ..auth import email as email_svc
from ..auth.permissions import has_capability, CAN_VIEW_USERS_TAB, CAN_VIEW_SERVER_TAB, CAN_VIEW_OWN_FILES
from ..deps import get_current_user_optional, get_db
from ..models import (
    ApprovalRequest, ApprovalStatus, Role, ServerSettings, Status, TokenPurpose, User,
)
from ..auth import discord as discord_svc
from ..services import audit

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


# Capability constants the template might want to gate on. Computed
# once at import; each is fed through has_capability() per request so
# custom-role overlays + per-user grants are reflected accurately.
_TEMPLATE_CAPS: tuple[str, ...] = tuple(
    v for k, v in vars(_perms).items()
    if isinstance(v, str) and k.startswith("CAN_")
)


def _common_ctx(request: Request, user: Optional[User], db: Session) -> dict:
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    caps: list[str] = []
    if user is not None:
        caps = sorted(c for c in _TEMPLATE_CAPS if has_capability(user, c))
    return {
        "request": request,
        "user": user,
        "caps": caps,
        "allow_signup": bool(s.allow_signup) if s else False,
        "password_signin_enabled": bool(s.password_signin_enabled) if s else True,
        "require_name_at_signup": bool(getattr(s, "require_name_at_signup", False)) if s else False,
        # Profile-page "Reset password" routes through an email-link
        # verification step when this is on. Only meaningful when SMTP
        # is healthy; the admin UI greys the toggle out otherwise.
        "password_reset_via_email": bool(getattr(s, "password_reset_via_email", False)) if s else False,
        # Brand identity for the nav. Falls back to the legacy
        # "VITRIOL → vitriol.rocks" pairing when the operator hasn't
        # customized them (NULL columns or no row yet).
        "brand_title": (getattr(s, "brand_title", None) if s else None) or "VITRIOL",
        "brand_link": (getattr(s, "brand_link", None) if s else None) or "https://vitriol.rocks",
        # Server-level border control — injected into every page so the
        # MutationObserver in border.js picks up the right initial state.
        "server_show_border": bool(getattr(s, "server_show_border", True)) if s else True,
        "server_border_style": str(getattr(s, "server_border_style", None) or "vitriol") if s else "vitriol",
        "server_default_theme": (getattr(s, "server_default_theme", None) or None) if s else None,
        "server_allow_user_theme": bool(getattr(s, "server_allow_user_theme", True)) if s else True,
        "support_email": (getattr(s, "support_email", None) or None) if s else None,
        "support_discord_url": (getattr(s, "support_discord_url", None) or None) if s else None,
        "support_discord_shared": bool(getattr(s, "support_discord_shared", False)) if s else False,
        "show_limit_hit_notice": bool(getattr(s, "show_limit_hit_notice", True)) if s else True,
        "show_user_support_button": bool(getattr(s, "show_user_support_button", False)) if s else False,
        "show_users_tab": user is not None and has_capability(user, CAN_VIEW_USERS_TAB),
        "show_server_tab": user is not None and has_capability(user, CAN_VIEW_SERVER_TAB),
        "show_files_tab": user is not None and has_capability(user, CAN_VIEW_OWN_FILES),
        # Single source of truth for the app version — comes from
        # web/__init__.py:__version__. Templates use this for:
        #   1. Static asset cache-busting URLs (?v={{ v }}) via base.html
        #   2. The status-bar "v1.x.y" badge in app.html
        # Bumping __version__ invalidates browser caches automatically.
        "version": __version__,
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


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request, db: Session = Depends(get_db)):
    """Anonymous-accessible — enter username/email, server emails a
    token-bearing reset link. Both halves of the round trip (this
    page + the /reset target the email links to) lived in unused
    backend endpoints until now; this page closes the loop the dead
    signin "Forgot password?" link was pointing at."""
    return templates.TemplateResponse(request, "forgot.html", _common_ctx(request, None, db))


@router.get("/reset", response_class=HTMLResponse)
def reset_page(request: Request, db: Session = Depends(get_db)):
    """Anonymous-accessible — landing target for the password-reset
    email link. Reads the ?token=... query param, posts new password
    to /auth/password-reset/confirm. Same page services both:
      - the traditional forgot-password flow (signed-out user)
      - the profile-page "reset via email" flow when the operator has
        turned that toggle on
    """
    return templates.TemplateResponse(request, "reset.html", _common_ctx(request, None, db))


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    if user is None:
        return RedirectResponse(url="/signin")
    return templates.TemplateResponse(request, "profile.html", _common_ctx(request, user, db))


@router.get("/files", response_class=HTMLResponse)
def files_page(request: Request, user: Optional[User] = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    if user is None:
        return RedirectResponse(url="/signin")
    if not has_capability(user, CAN_VIEW_OWN_FILES):
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "files.html", _common_ctx(request, user, db))


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


# ---- Email-verification landing page --------------------------------------
#
# Verification emails point at this UI route instead of the bare JSON API
# (/api/v1/auth/verify) so a click renders a real success/failure page
# rather than dumping `{"message":"..."}`. We do the same work as the API
# route: consume the token, flip the user's status, fan out admin
# notifications when a previously-unverified pending user is promoted.

@router.get("/verify", response_class=HTMLResponse)
async def verify_email_page(
    request: Request,
    token: str = "",
    user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    ctx = _common_ctx(request, user, db)
    if not token:
        ctx.update({"ok": False, "headline": "Missing token",
                    "detail": "The verification link didn't include a token. Check your email for the full URL."})
        return templates.TemplateResponse(request, "verify.html", ctx, status_code=400)

    row = email_svc.consume_verification_token(db, token)
    if row is None:
        ctx.update({"ok": False, "headline": "Link is invalid or expired",
                    "detail": "Verification links expire after 24 hours. Sign up again to get a new one."})
        return templates.TemplateResponse(request, "verify.html", ctx, status_code=400)

    target = db.query(User).get(row.user_id)
    if target is None:
        ctx.update({"ok": False, "headline": "Account no longer exists",
                    "detail": "The account this link belongs to has been removed."})
        return templates.TemplateResponse(request, "verify.html", ctx, status_code=404)

    just_promoted = False
    if row.purpose == TokenPurpose.signup:
        target.email_verified_at = datetime.utcnow()
        if target.status == Status.unverified:
            target.status = Status.active
            just_promoted = True
    elif row.purpose == TokenPurpose.change_email and row.new_email:
        target.email = row.new_email
        target.email_verified_at = datetime.utcnow()
    db.commit()
    audit.log(db, target.id, "verify_email", target_user_id=target.id)

    # Fire deferred pending-approval notifications now that ownership is proven.
    if just_promoted and target.role == Role.pending:
        ar = ApprovalRequest(
            user_id=target.id, requested_role=Role.user, status=ApprovalStatus.pending,
        )
        db.add(ar)
        db.commit()
        admins = (
            db.query(User)
            .filter(User.role.in_([Role.super_admin, Role.admin]))
            .filter(User.email.isnot(None))
            .all()
        )
        await email_svc.send_pending_approval_notification(
            db, target, [a.email for a in admins if a.email],
        )
        from ..services.notifications import notify_all
        await notify_all(
            db,
            f":hourglass: New pending user **{target.username}** ({target.email}) awaits approval.",
        )

    if target.role == Role.pending:
        headline = "Email verified — pending approval"
        detail = ("Your email is confirmed. An administrator has been notified and "
                  "will review your sign-up. You'll get an email once approved.")
    else:
        headline = "Email verified"
        detail = "You can sign in now."
    ctx.update({"ok": True, "headline": headline, "detail": detail})
    return templates.TemplateResponse(request, "verify.html", ctx)
