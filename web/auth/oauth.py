"""OAuth (Google, GitHub, generic OIDC) via Authlib.

Provider creds live in `server_settings`; SSO can be reconfigured at
runtime without a restart. The generic `oidc` slot uses OpenID Connect
discovery (`.well-known/openid-configuration`), so anything that speaks
OIDC — Authentik, Keycloak, Auth0, Okta, Zitadel, etc. — works with
just an issuer URL + client id/secret.
"""
from __future__ import annotations
from typing import Optional

from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session

from ..models import ServerSettings
from .crypto import decrypt


def _provider_meta(s: ServerSettings) -> list[dict]:
    """Public-facing list — what to show on the sign-in page."""
    out: list[dict] = []
    if s.oauth_google_client_id and s.oauth_google_client_secret_enc:
        out.append({"id": "google", "label": "Continue with Google"})
    if s.oauth_github_client_id and s.oauth_github_client_secret_enc:
        out.append({"id": "github", "label": "Continue with GitHub"})
    if (s.oidc_enabled and s.oidc_issuer
            and s.oidc_client_id and s.oidc_client_secret_enc):
        label = s.oidc_display_name or "Continue with SSO"
        out.append({"id": "oidc", "label": label})
    return out


def list_providers(db: Session) -> list[dict]:
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None:
        return []
    return _provider_meta(s)


def build_oauth(db: Session) -> tuple[OAuth, dict]:
    """Returns (oauth, registered) where `registered` maps provider name → True."""
    oauth = OAuth()
    registered: dict[str, bool] = {}

    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None:
        return oauth, registered

    if s.oauth_google_client_id and s.oauth_google_client_secret_enc:
        oauth.register(
            name="google",
            client_id=s.oauth_google_client_id,
            client_secret=decrypt(s.oauth_google_client_secret_enc),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        registered["google"] = True

    if s.oauth_github_client_id and s.oauth_github_client_secret_enc:
        oauth.register(
            name="github",
            client_id=s.oauth_github_client_id,
            client_secret=decrypt(s.oauth_github_client_secret_enc),
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "user:email"},
        )
        registered["github"] = True

    if (s.oidc_enabled and s.oidc_issuer
            and s.oidc_client_id and s.oidc_client_secret_enc):
        # OIDC discovery — Authlib pulls authorization, token, userinfo,
        # and JWKS endpoints from the .well-known doc. The issuer URL
        # may or may not include the /.well-known suffix; we add it if
        # missing so admins can paste either form.
        issuer = s.oidc_issuer.rstrip("/")
        if issuer.endswith("/.well-known/openid-configuration"):
            metadata_url = issuer
        else:
            metadata_url = f"{issuer}/.well-known/openid-configuration"
        scopes = s.oidc_scopes or "openid email profile"
        oauth.register(
            name="oidc",
            client_id=s.oidc_client_id,
            client_secret=decrypt(s.oidc_client_secret_enc),
            server_metadata_url=metadata_url,
            client_kwargs={"scope": scopes},
        )
        registered["oidc"] = True

    return oauth, registered
