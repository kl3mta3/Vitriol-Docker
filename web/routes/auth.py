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
    if user.status == Status.unverified:
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before signing in. Check your inbox for the verification link.",
        )
    if user.status == Status.suspended and user.suspended_until and user.suspended_until > datetime.utcnow():
        raise HTTPException(status_code=403, detail=f"Account suspended until {user.suspended_until.isoformat()}Z")
    # Block sign-in for unverified users when the operator requires email
    # verification. Bootstrap super-admins (created from env or wizard) are
    # exempt — they're the only path back into the app if SMTP breaks. SSO
    # users are also exempt because the IdP already vouched for the email.
    s = db.query(ServerSettings).get(1)
    if (
        s is not None
        and bool(s.require_email_verification)
        and user.email_verified_at is None
        and user.role != Role.super_admin
        and user.password_hash  # password-based sign-in only; SSO has no hash
    ):
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before signing in. Check your inbox for the verification link.",
        )
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

    # Two distinct creation paths:
    #
    #   verify ON  → row goes in as Status.unverified. Hidden from admin
    #                user lists. Cleanup task purges after 24h. No Discord
    #                ping, no admin email, no approval row — those fire
    #                from the verify endpoint when the user proves email
    #                ownership. If the verification email itself fails to
    #                send, we delete the row so the user can retry once
    #                SMTP is fixed instead of being orphaned.
    #
    #   verify OFF → fire the original flow: row goes in as active, then
    #                Discord + admin emails go out for pending users.
    user = User(
        username=req.username,
        email=req.email,
        password_hash=hash_password(req.password),
        role=Role.pending if role == Role.pending else role,
        status=Status.unverified if require_verify else Status.active,
        email_verified_at=None if require_verify else datetime.utcnow(),
        custom_role_id=s.signup_default_custom_role_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    if require_verify:
        raw = email_svc.issue_verification_token(db, user, TokenPurpose.signup)
        ok = await email_svc.send_verification_email(db, user, raw)
        if not ok:
            # SMTP rejected the message (or isn't configured). Don't leave
            # an unverified row that the user can never reach — delete and
            # surface the failure so they can retry. The container log
            # already captured the underlying SMTP error via _send().
            db.delete(user)
            db.commit()
            raise HTTPException(
                status_code=503,
                detail=(
                    "We couldn't send the verification email. The server's "
                    "outbound mail isn't working — please contact the site "
                    "administrator and try again later."
                ),
            )
        audit.log(db, None, "signup_unverified", target_user_id=user.id,
                  metadata={"role": user.role.value})
        return MessageResponse(message=(
            "Account pending email verification. Check your inbox for the "
            "link — it expires in 24 hours. You won't appear in the user "
            "list until you verify."
        ))

    # ---- verify=OFF path: full flow happens immediately ------------------
    if user.role == Role.pending:
        ar = ApprovalRequest(user_id=user.id, requested_role=Role.user, status=ApprovalStatus.pending)
        db.add(ar)
        db.commit()
        await _notify_admins_of_pending(db, user)

    audit.log(db, None, "signup", target_user_id=user.id, metadata={"role": user.role.value})

    if user.role == Role.pending:
        msg = ("Account created and pending approval. An administrator will review "
               "your sign-up shortly; you'll be able to sign in once approved. "
               "Please allow a bit of time.")
    else:
        msg = "Account created. You can sign in now."
    return MessageResponse(message=msg)


async def _notify_admins_of_pending(db: Session, user: User) -> None:
    """Fire Discord webhook + admin/super-admin emails for a newly-pending
    user. Called from signup (verify=off path) and from verify() (verify=on
    path) — both end with the same notification fan-out."""
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


@router.get("/verify", response_model=MessageResponse)
async def verify(token: str, db: Session = Depends(get_db)):
    row = email_svc.consume_verification_token(db, token)
    if row is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user: Optional[User] = db.query(User).get(row.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User missing")

    just_promoted_from_unverified = False
    if row.purpose == TokenPurpose.signup:
        user.email_verified_at = datetime.utcnow()
        # Sign-up verification: flip the limbo `unverified` status into a
        # real account state. From here on the row appears in the admin
        # user list, can sign in, and (if pending-role) is eligible for
        # the approval queue.
        if user.status == Status.unverified:
            user.status = Status.active
            just_promoted_from_unverified = True
    elif row.purpose == TokenPurpose.change_email and row.new_email:
        user.email = row.new_email
        user.email_verified_at = datetime.utcnow()
    db.commit()
    audit.log(db, user.id, "verify_email", target_user_id=user.id)

    # Now that the user has proven email ownership, do the pending fan-out
    # that signup() deferred. This is the moment Discord + admin emails
    # fire — not at signup time — so admins don't get pinged about
    # signups that never complete verification.
    if just_promoted_from_unverified and user.role == Role.pending:
        ar = ApprovalRequest(
            user_id=user.id, requested_role=Role.user, status=ApprovalStatus.pending,
        )
        db.add(ar)
        db.commit()
        await _notify_admins_of_pending(db, user)

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

    Resolution order (first hit wins):
      1. ``public_base_url`` from server settings — operator-configured,
         most authoritative source. Set it to the URL users actually type
         in the browser (e.g. ``https://app.vitriol.rocks``).
      2. ``X-Forwarded-Host`` + ``X-Forwarded-Proto`` from the request —
         what Coolify / Caddy / Traefik / nginx forward when terminating
         TLS for us. The ``ProxyHeadersMiddleware`` (uvicorn ``--proxy-headers``)
         normally rewrites ``request.url`` from these, so reading them
         directly is just belt-and-suspenders for misconfigured stacks.
      3. ``request.url_for(...)``. This is last because behind a proxy
         it can produce ``http://internal-host:3825/...`` which never
         matches the IdP's registered URI.
    """
    s = db.query(ServerSettings).filter(ServerSettings.id == 1).first()
    base = (s.public_base_url if s and s.public_base_url else "").strip().rstrip("/")
    if base:
        return f"{base}/api/v1/auth/sso/{provider}/callback"

    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    fwd_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if fwd_host:
        # Strip any accidental "https://" prefix the proxy might forward.
        if "://" in fwd_host:
            fwd_host = fwd_host.split("://", 1)[1]
        fwd_host = fwd_host.split(",", 1)[0].strip()  # XFH can be a chain
        return f"{fwd_proto}://{fwd_host}/api/v1/auth/sso/{provider}/callback"

    return str(request.url_for("sso_callback", provider=provider))


@router.get("/sso/{provider}/start")
async def sso_start(provider: str, request: Request, db: Session = Depends(get_db)):
    """Start the OAuth/OIDC dance for a named provider.

    `provider` is the registry slug — `google`, `github`, or any of the
    OIDC slugs the super admin defined under Server settings. The
    `oauth.<slug>.authorize_redirect` call generates the IdP-specific
    authorization URL and 302s the browser there.

    Diagnostic mode: when ``?debug=1`` is passed, returns a plain-text
    page showing the exact redirect_uri that would be sent to the IdP
    plus the public_base_url it was derived from — invaluable for
    diagnosing redirect_uri_mismatch errors without round-tripping to
    Google.
    """
    oauth, registered = build_oauth(db)
    if provider not in registered:
        raise HTTPException(status_code=404, detail="SSO provider not configured")
    redirect_uri = _sso_redirect_uri(request, db, provider)

    if request.query_params.get("debug") == "1":
        s = db.query(ServerSettings).filter(ServerSettings.id == 1).first()
        body = (
            "Vitriol SSO debug — no redirect performed.\n\n"
            f"provider:                {provider}\n"
            f"public_base_url (DB):    {s.public_base_url if s else '(no settings row)'!r}\n"
            f"redirect_uri (sent):     {redirect_uri}\n"
            f"request.url_for fallback: {str(request.url_for('sso_callback', provider=provider))!r}\n"
            f"request Host header:     {request.headers.get('host')!r}\n"
            f"request scheme:          {request.url.scheme!r}\n"
            "\n"
            "Paste the 'redirect_uri (sent)' line into the IdP's allowed\n"
            "redirect URIs list verbatim — character-for-character — and\n"
            "the redirect_uri_mismatch error will go away.\n"
        )
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(body)

    # Test mode: stash a flag in the session so the callback can render
    # a "test successful" page WITHOUT creating a user, linking an
    # OAuthIdentity, or issuing session cookies. The flag rides through
    # the whole IdP round-trip in the same session that Authlib uses to
    # track OAuth state, so it's automatically scoped to this single
    # auth attempt.
    if request.query_params.get("test") == "1":
        request.session["sso_test"] = provider

    return await getattr(oauth, provider).authorize_redirect(request, redirect_uri)


@router.get("/sso/{provider}/callback", name="sso_callback")
async def sso_callback(
    provider: str, request: Request, response: Response, db: Session = Depends(get_db),
):
    import logging as _logging
    _sso_log = _logging.getLogger("vitriol.sso")

    oauth, registered = build_oauth(db)
    if provider not in registered:
        raise HTTPException(status_code=404, detail="SSO provider not configured")
    kind = registered[provider]   # 'google' | 'github' | 'oidc'
    client = getattr(oauth, provider)

    # Token exchange. Authlib pulls the redirect_uri it stashed during the
    # start step out of the session and re-sends it; if that's missing
    # (samesite cookie dropped, session middleware misconfigured) the call
    # blows up with a MismatchingStateError. Surface a real message rather
    # than a bare 500 so the operator can fix the config.
    try:
        token = await client.authorize_access_token(request)
    except Exception as e:
        _sso_log.exception("SSO token exchange failed for provider=%s", provider)
        raise HTTPException(
            status_code=502,
            detail=f"SSO token exchange failed ({type(e).__name__}): {e}",
        )

    sub: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    try:
        if kind == "google":
            info = token.get("userinfo") or await client.userinfo(token=token)
            sub = info.get("sub")
            email = info.get("email")
            name = info.get("name") or info.get("given_name")
        elif kind == "github":
            # GitHub doesn't issue an OIDC id_token — fetch the profile
            # via the REST API. The Authlib ``client.get`` call uses the
            # access token from the OAuth dance.
            resp = await client.get("user", token=token)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"GitHub /user returned {resp.status_code}: {resp.text[:200]}"
                )
            info = resp.json() if resp.text else {}
            sub = str(info.get("id") or "")
            name = info.get("login")
            if not info.get("email"):
                er = await client.get("user/emails", token=token)
                if er.status_code >= 400:
                    # Common cause: the OAuth app doesn't have the
                    # `user:email` scope. Fall through with email=None
                    # rather than 500'ing — name+sub is enough to create
                    # the account, and email can be added later.
                    _sso_log.warning(
                        "GitHub /user/emails returned %s — proceeding without email. "
                        "Add 'user:email' to the OAuth app scopes to capture it.",
                        er.status_code,
                    )
                else:
                    for entry in (er.json() or []):
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
    except HTTPException:
        raise
    except Exception as e:
        _sso_log.exception("SSO profile fetch failed for provider=%s", provider)
        raise HTTPException(
            status_code=502,
            detail=f"SSO profile fetch failed ({type(e).__name__}): {e}",
        )

    if not sub:
        raise HTTPException(status_code=400, detail="SSO provider returned no subject id")

    # Test mode: the start endpoint stamped session["sso_test"] = provider
    # so we can detect a "Test sign-in" round-trip vs. a real one. Render
    # a styled success page (no user creation, no identity link, no
    # session cookies) and bail. The operator sees a green "Test passed"
    # screen with the IdP's reported sub/email/name so they can confirm
    # the round-trip worked end-to-end, without polluting the user list
    # with a pending account.
    if request.session.pop("sso_test", None) == provider:
        from fastapi.templating import Jinja2Templates
        _templates = Jinja2Templates(directory="web/templates")
        return _templates.TemplateResponse(
            request,
            "sso_test_result.html",
            {
                "request": request,
                "provider": provider,
                "kind": kind,
                "sub": sub,
                "email": email or "(not provided)",
                "name": name or "(not provided)",
            },
        )

    identity: Optional[OAuthIdentity] = (
        db.query(OAuthIdentity)
        .filter(OAuthIdentity.provider == provider, OAuthIdentity.subject == sub)
        .one_or_none()
    )
    if identity is not None:
        # Existing identity row — sign in as the linked user.
        user = db.query(User).get(identity.user_id)
        if user is None:
            raise HTTPException(status_code=500, detail="Orphaned OAuth identity")
        if user.status == Status.banned:
            raise HTTPException(status_code=403, detail="Account banned")
    else:
        # No identity row yet for this (provider, sub). CRITICAL: before
        # creating a brand-new account, see if this email already belongs
        # to a registered user — if so, link the SSO identity to THAT
        # account instead of duplicating it as a pending signup. This is
        # what prevents the "I logged in via Google with my super-admin
        # email and got bounced into a pending limbo account" footgun.
        # We only do this lookup when the IdP gave us a usable email
        # (Google always does; GitHub only does when the OAuth app has
        # the user:email scope) — without an email there's nothing to
        # match against.
        existing: Optional[User] = None
        if email:
            existing = (
                db.query(User)
                .filter(User.email == email)
                .filter(User.status != Status.banned)
                .one_or_none()
            )

        if existing is not None:
            # Link the new SSO identity to the existing user. The IdP has
            # already proven the email belongs to whoever just authed —
            # treat them as the rightful owner of that account.
            user = existing
            identity = OAuthIdentity(
                user_id=user.id, provider=provider, subject=sub, email=email,
            )
            db.add(identity)
            # If the existing user wasn't email-verified yet, the IdP
            # vouching is good enough — flip the flag so they don't get
            # nagged on next sign-in.
            if user.email_verified_at is None:
                user.email_verified_at = datetime.utcnow()
            # Promote out of `unverified` status the same way the verify
            # endpoint does — IdP attestation is equivalent to clicking
            # the verification link.
            if user.status == Status.unverified:
                user.status = Status.active
            db.commit()
        else:
            # Truly new — create a fresh account with the configured
            # signup default role. (This path is what makes "Sign up via
            # Google" work for first-time users.)
            s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
            default_role = (s.signup_default_role if s else Role.viewer) or Role.viewer
            username = (name or f"{provider}_{sub}")[:64]
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
            identity = OAuthIdentity(
                user_id=user.id, provider=provider, subject=sub, email=email,
            )
            db.add(identity)
            db.commit()

    user.last_login_at = datetime.utcnow()
    db.commit()
    access, _ = issue_access_token(user.id, user.role.value)
    raw_refresh, _, _ = new_refresh_token()
    _record_session(db, user, raw_refresh, request)
    redirect = RedirectResponse(url="/")
    _set_session_cookies(redirect, access, raw_refresh)
    return redirect
