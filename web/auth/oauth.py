"""OAuth (Google, GitHub, plus N OpenID Connect providers) via Authlib.

Google + GitHub are singletons configured in `server_settings`. OIDC is
a list — operators can register multiple IdPs (e.g. Authentik for staff,
Auth0 for customers) and each shows up as its own button on the sign-in
page. Each OIDC row's `slug` becomes the URL fragment in its callback
(`/api/v1/auth/sso/<slug>/callback`).

`build_oauth(db)` returns an Authlib `OAuth` instance with one client
per configured-and-enabled provider, keyed by its slug. The slug
namespace is shared with the hard-coded `google` / `github` names —
operators are advised not to use those slugs for OIDC entries (the
sign-in dispatcher would prefer the OIDC row, which is fine, but the
behavioral overload is confusing).
"""
from __future__ import annotations
from typing import Optional

from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session

from ..models import OidcProvider, ServerSettings
from .crypto import decrypt


def list_providers(db: Session) -> list[dict]:
    """Public — what to render on the sign-in page. Returns one entry per
    configured-and-enabled provider in this order: google, github, then
    each OIDC row sorted by display_name. Each entry is
    ``{id, label, kind, show_on_signup}`` where ``show_on_signup``
    tells the signup page whether to include the button.

    Signin page renders everything in this list. The signup page
    filters by ``show_on_signup`` client-side — keeps the API surface
    simple and lets the operator flip a button between pages by
    toggling one column.
    """
    out: list[dict] = []
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is not None:
        if (
            bool(s.oauth_google_enabled)
            and s.oauth_google_client_id
            and s.oauth_google_client_secret_enc
        ):
            out.append({
                "id": "google",
                "label": "Continue with Google",
                "kind": "google",
                "show_on_signup": bool(getattr(s, "oauth_google_show_on_signup", True)),
            })
        if (
            bool(s.oauth_github_enabled)
            and s.oauth_github_client_id
            and s.oauth_github_client_secret_enc
        ):
            out.append({
                "id": "github",
                "label": "Continue with GitHub",
                "kind": "github",
                "show_on_signup": bool(getattr(s, "oauth_github_show_on_signup", True)),
            })

    rows = (
        db.query(OidcProvider)
        .filter(OidcProvider.enabled.is_(True))
        .order_by(OidcProvider.display_name)
        .all()
    )
    for r in rows:
        if not (r.issuer and r.client_id and r.client_secret_enc):
            continue
        out.append({
            "id": r.slug,
            "label": r.display_name or "Continue with SSO",
            "kind": "oidc",
            "show_on_signup": bool(getattr(r, "show_on_signup", True)),
        })
    return out


def _normalize_metadata_url(issuer: str) -> str:
    """OIDC discovery URL from issuer. Accepts the issuer with or without
    the `.well-known/openid-configuration` suffix, since some IdP UIs
    show one and some show the other."""
    issuer = issuer.rstrip("/")
    if issuer.endswith("/.well-known/openid-configuration"):
        return issuer
    return f"{issuer}/.well-known/openid-configuration"


def build_oauth(db: Session) -> tuple[OAuth, dict]:
    """Returns (oauth, registered) where `registered` maps each
    configured provider's slug → its `kind` ('google'|'github'|'oidc')."""
    oauth = OAuth()
    registered: dict[str, str] = {}

    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)

    if (
        s is not None
        and bool(s.oauth_google_enabled)
        and s.oauth_google_client_id
        and s.oauth_google_client_secret_enc
    ):
        oauth.register(
            name="google",
            client_id=s.oauth_google_client_id,
            client_secret=decrypt(s.oauth_google_client_secret_enc),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        registered["google"] = "google"

    if (
        s is not None
        and bool(s.oauth_github_enabled)
        and s.oauth_github_client_id
        and s.oauth_github_client_secret_enc
    ):
        oauth.register(
            name="github",
            client_id=s.oauth_github_client_id,
            client_secret=decrypt(s.oauth_github_client_secret_enc),
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "user:email"},
        )
        registered["github"] = "github"

    rows = (
        db.query(OidcProvider)
        .filter(OidcProvider.enabled.is_(True))
        .all()
    )
    for r in rows:
        if not (r.issuer and r.client_id and r.client_secret_enc):
            continue
        if r.slug in registered:
            # Slug collision with google/github or another OIDC row —
            # skip rather than overload behaviour. The CRUD layer
            # rejects collisions on save, so this only happens via
            # external DB tampering.
            continue
        try:
            oauth.register(
                name=r.slug,
                client_id=r.client_id,
                client_secret=decrypt(r.client_secret_enc),
                server_metadata_url=_normalize_metadata_url(r.issuer),
                client_kwargs={"scope": r.scopes or "openid email profile"},
            )
            registered[r.slug] = "oidc"
        except Exception:
            # A bad issuer URL or unreachable discovery doc shouldn't
            # crash the rest of the auth surface. The caller will hit a
            # 404 / 502 when actually starting the flow for this slug,
            # which is the right place to surface the error.
            continue

    return oauth, registered
