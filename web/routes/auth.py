"""Sign-in / sign-up / OAuth / verification routes."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import email as email_svc
from ..auth.jwt import issue_access_token, new_refresh_token, hash_refresh
from ..auth.password import hash_password, verify_password, needs_rehash
from ..auth.oauth import build_oauth, list_providers
from ..config import get_settings
from ..deps import get_db
from ..models import (
    ApprovalRequest, ApprovalStatus, OAuthIdentity, Role, ServerSettings,
    Session_ as SessionRow, Status, TokenPurpose, User,
)
from ..schemas import (
    MessageResponse, PasswordResetConfirmRequest, PasswordResetRequest,
    SignInRequest, SignUpRequest, TokenResponse,
)
from ..services import audit
from ..auth import discord as discord_svc

router = APIRouter(prefix="/auth", tags=["auth"])
_settings = get_settings()


def _set_session_cookies(resp: Response, access: str, refresh: str) -> None:
    resp.set_cookie(
        "vitriol_access", access,
        httponly=True, samesite="lax", secure=False,
        max_age=_settings.access_token_minutes * 60, path="/",
    )
    resp.set_cookie(
        "vitriol_refresh", refresh,
        httponly=True, samesite="lax", secure=False,
        max_age=_settings.refresh_token_days * 86400, path="/api/v1/auth",
    )


def _clear_session_cookies(resp: Response) -> None:
    resp.delete_cookie("vitriol_access", path="/")
    resp.delete_cookie("vitriol_refresh", path="/api/v1/auth")


def _record_session(db: Session, user: User, refresh_raw: str, request: Request) -> None:
    raw, h, exp = refresh_raw, hash_refresh(refresh_raw), None
    from datetime import timedelta
    exp = datetime.utcnow() + timedelta(days=_settings.refresh_token_days)
    row = SessionRow(
        user_id=user.id, refresh_token_hash=h,
        user_agent=(request.headers.get("user-agent") or "")[:255],
        ip=(request.client.host if request.client else None),
        expires_at=exp,
    )
    db.add(row)
    db.commit()


# ------------------------------------------------------------- sign in

@router.post("/signin", response_model=TokenResponse)
def signin(req: SignInRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    ident = req.identifier.strip()
    user: Optional[User] = (
        db.query(User)
        .filter((User.username == ident) | (User.email == ident))
        .one_or_none()
    )
    if user is None or not verify_password(req.password, user.password_hash or ""):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.status == Status.banned:
        raise HTTPException(status_code=403, detail="Account banned")
    if user.status == Status.suspended and user.suspended_until and user.suspended_until > datetime.utcnow():
        raise HTTPException(status_code=403, detail=f"Account suspended until {user.suspended_until.isoformat()}Z")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(req.password)
    user.last_login_at = datetime.utcnow()
    db.commit()
    access, expires_in = issue_access_token(user.id, user.role.value)
    raw_refresh, _, _ = new_refresh_token()
    _record_session(db, user, raw_refresh, request)
    _set_session_cookies(response, access, raw_refresh)
    audit.log(db, user.id, "signin")
    return TokenResponse(access_token=access, expires_in=expires_in)


@router.post("/logout", response_model=MessageResponse)
def logout(response: Response):
    _clear_session_cookies(response)
    return MessageResponse(message="Signed out")


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    raw = request.cookies.get("vitriol_refresh")
    if not raw:
        raise HTTPException(status_code=401, detail="No refresh token")
    h = hash_refresh(raw)
    row: Optional[SessionRow] = db.query(SessionRow).filter(SessionRow.refresh_token_hash == h).one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user: Optional[User] = db.query(User).get(row.user_id)
    if user is None or user.status == Status.banned:
        raise HTTPException(status_code=403, detail="User unavailable")
    access, expires_in = issue_access_token(user.id, user.role.value)
    response.set_cookie(
        "vitriol_access", access,
        httponly=True, samesite="lax", secure=False,
        max_age=_settings.access_token_minutes * 60, path="/",
    )
    return TokenResponse(access_token=access, expires_in=expires_in)


# ------------------------------------------------------------- sign up

@router.post("/signup", response_model=MessageResponse)
async def signup(req: SignUpRequest, db: Session = Depends(get_db)):
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None or not s.allow_signup:
        raise HTTPException(status_code=403, detail="Sign-up is disabled")
    if db.query(User).filter(User.username == req.username).count():
        raise HTTPException(status_code=409, detail="Username taken")
    if db.query(User).filter(User.email == req.email).count():
        raise HTTPException(status_code=409, detail="Email already registered")
    role = s.signup_default_role or Role.viewer
    require_verify = bool(s.require_email_verification)
    user = User(
        username=req.username,
        email=req.email,
        password_hash=hash_password(req.password),
        role=Role.pending if role == Role.pending else role,
        status=Status.active,
        # When verification is off, mark verified at creation so the user
        # never gets nagged for it later.
        email_verified_at=None if require_verify else datetime.utcnow(),
        # Apply the configured custom-role overlay if one is set.
        custom_role_id=s.signup_default_custom_role_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    if require_verify:
        raw = email_svc.issue_verification_token(db, user, TokenPurpose.signup)
        await email_svc.send_verification_email(db, user, raw)

    if user.role == Role.pending:
        ar = ApprovalRequest(user_id=user.id, requested_role=Role.user, status=ApprovalStatus.pending)
        db.add(ar)
        db.commit()
        admins = (
            db.query(User)
            .filter(User.role.in_([Role.super_admin, Role.admin]))
            .filter(User.email.isnot(None))
            .all()
        )
        recipients = [a.email for a in admins if a.email]
        await email_svc.send_pending_approval_notification(db, user, recipients)
        await discord_svc.notify(
            db, f":hourglass: New pending user **{user.username}** ({user.email}) awaits approval."
        )

    audit.log(db, None, "signup", target_user_id=user.id, metadata={"role": user.role.value})

    # Build a message that matches what actually happened. Four flavors:
    #   verify on  + pending  → check email AND wait for approval
    #   verify on  + active   → check email
    #   verify off + pending  → wait for approval
    #   verify off + active   → ready to sign in now
    if user.role == Role.pending and require_verify:
        msg = ("Account created. Verify your email — we've sent you a link — and an "
               "administrator will review your sign-up. You'll be notified by email "
               "once approved. Please allow a bit of time.")
    elif user.role == Role.pending:
        msg = ("Account created and pending approval. An administrator will review "
               "your sign-up shortly; you'll be able to sign in once approved. "
               "Please allow a bit of time.")
    elif require_verify:
        msg = "Check your email to verify your account."
    else:
        msg = "Account created. You can sign in now."
    return MessageResponse(message=msg)


@router.get("/verify", response_model=MessageResponse)
def verify(token: str, db: Session = Depends(get_db)):
    row = email_svc.consume_verification_token(db, token)
    if row is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user: Optional[User] = db.query(User).get(row.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User missing")
    if row.purpose == TokenPurpose.signup:
        user.email_verified_at = datetime.utcnow()
    elif row.purpose == TokenPurpose.change_email and row.new_email:
        user.email = row.new_email
        user.email_verified_at = datetime.utcnow()
    db.commit()
    audit.log(db, user.id, "verify_email", target_user_id=user.id)
    return MessageResponse(message="Email verified.")


# ----------------------------------------------------- password reset

@router.post("/password-reset", response_model=MessageResponse)
async def password_reset(req: PasswordResetRequest, db: Session = Depends(get_db)):
    user: Optional[User] = (
        db.query(User)
        .filter((User.username == req.identifier) | (User.email == req.identifier))
        .one_or_none()
    )
    # Don't leak account existence — always return the same message.
    if user is not None and user.email:
        raw = email_svc.issue_password_reset_token(db, user)
        await email_svc.send_password_reset_email(db, user, raw)
    return MessageResponse(message="If that account exists, a reset email is on its way.")


@router.post("/password-reset/confirm", response_model=MessageResponse)
def password_reset_confirm(req: PasswordResetConfirmRequest, db: Session = Depends(get_db)):
    row = email_svc.consume_password_reset_token(db, req.token)
    if row is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user: Optional[User] = db.query(User).get(row.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User missing")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    audit.log(db, user.id, "password_reset_confirm", target_user_id=user.id)
    return MessageResponse(message="Password updated.")


# ----------------------------------------------------------------- SSO

@router.get("/sso/providers")
def sso_providers(db: Session = Depends(get_db)):
    """Public — the sign-in page renders one button per item."""
    return list_providers(db)


@router.get("/policy")
def auth_policy(db: Session = Depends(get_db)):
    """Public — what the sign-in page needs to know without an account.

    Drives the dynamic Sign up link and the SSO buttons. Returned data
    is intentionally non-sensitive: a flag and a public providers list.
    """
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    return {
        "allow_signup": bool(s.allow_signup) if s else False,
        "signup_default_role": (s.signup_default_role.value if s and hasattr(s.signup_default_role, "value")
                                 else (str(s.signup_default_role) if s else "viewer")),
        "providers": list_providers(db),
    }


def _sso_redirect_uri(request: Request, db: Session, provider: str) -> str:
    """Build the redirect URI Google/GitHub/OIDC must redirect back to.

    Prefers the operator-configured ``public_base_url`` so the URL exactly
    matches what was registered with the IdP. Falls back to
    ``request.url_for()`` only when no public URL is set (dev/localhost).
    Behind a proxy ``request.url_for`` can produce ``http://internal-host``
    which never matches the IdP's registered URI.
    """
    s = db.query(ServerSettings).filter(ServerSettings.id == 1).first()
    base = (s.public_base_url if s and s.public_base_url else "").strip().rstrip("/")
    if base:
        return f"{base}/api/v1/auth/sso/{provider}/callback"
    return str(request.url_for("sso_callback", provider=provider))


@router.get("/sso/{provider}/start")
async def sso_start(provider: str, request: Request, db: Session = Depends(get_db)):
    """Start the OAuth/OIDC dance for a named provider.

    `provider` is the registry slug — `google`, `github`, or any of the
    OIDC slugs the super admin defined under Server settings. The
    `oauth.<slug>.authorize_redirect` call generates the IdP-specific
    authorization URL and 302s the browser there.
    """
    oauth, registered = build_oauth(db)
    if provider not in registered:
        raise HTTPException(status_code=404, detail="SSO provider not configured")
    redirect_uri = _sso_redirect_uri(request, db, provider)
    return await getattr(oauth, provider).authorize_redirect(request, redirect_uri)


@router.get("/sso/{provider}/callback", name="sso_callback")
async def sso_callback(
    provider: str, request: Request, response: Response, db: Session = Depends(get_db),
):
    oauth, registered = build_oauth(db)
    if provider not in registered:
        raise HTTPException(status_code=404, detail="SSO provider not configured")
    kind = registered[provider]   # 'google' | 'github' | 'oidc'
    client = getattr(oauth, provider)
    token = await client.authorize_access_token(request)

    sub: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    if kind == "google":
        info = token.get("userinfo") or await client.userinfo(token=token)
        sub = info.get("sub")
        email = info.get("email")
        name = info.get("name") or info.get("given_name")
    elif kind == "github":
        resp = await client.get("user", token=token)
        info = resp.json()
        sub = str(info.get("id"))
        name = info.get("login")
        if not info.get("email"):
            er = await client.get("user/emails", token=token)
            for entry in er.json():
                if entry.get("primary") and entry.get("verified"):
                    email = entry["email"]
                    break
        else:
            email = info["email"]
    elif kind == "oidc":
        # Any operator-defined OIDC provider (Authentik, Keycloak, Auth0,
        # Okta, Zitadel, ...). Authlib validates the ID token against
        # JWKS pulled from the discovery doc; `userinfo` returns the
        # standard OIDC claims set.
        info = token.get("userinfo")
        if info is None:
            try:
                info = await client.userinfo(token=token)
            except Exception:
                info = {}
        sub = info.get("sub") or info.get("subject")
        email = info.get("email")
        name = (
            info.get("preferred_username")
            or info.get("nickname")
            or info.get("name")
            or info.get("given_name")
        )
    if not sub:
        raise HTTPException(status_code=400, detail="SSO provider returned no subject id")

    identity: Optional[OAuthIdentity] = (
        db.query(OAuthIdentity)
        .filter(OAuthIdentity.provider == provider, OAuthIdentity.subject == sub)
        .one_or_none()
    )
    if identity is None:
        s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
        default_role = (s.signup_default_role if s else Role.viewer) or Role.viewer
        username = (name or f"{provider}_{sub}")[:64]
        # Disambiguate username collisions.
        i = 1
        while db.query(User).filter(User.username == username).count():
            username = f"{(name or provider)[:60]}_{i}"
            i += 1
        user = User(
            username=username, email=email,
            password_hash=None,
            role=default_role,
            status=Status.active,
            email_verified_at=datetime.utcnow() if email else None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        identity = OAuthIdentity(user_id=user.id, provider=provider, subject=sub, email=email)
        db.add(identity)
        db.commit()
    else:
        user = db.query(User).get(identity.user_id)
        if user is None:
            raise HTTPException(status_code=500, detail="Orphaned OAuth identity")
        if user.status == Status.banned:
            raise HTTPException(status_code=403, detail="Account banned")

    user.last_login_at = datetime.utcnow()
    db.commit()
    access, _ = issue_access_token(user.id, user.role.value)
    raw_refresh, _, _ = new_refresh_token()
    _record_session(db, user, raw_refresh, request)
    redirect = RedirectResponse(url="/")
    _set_session_cookies(redirect, access, raw_refresh)
    return redirect
