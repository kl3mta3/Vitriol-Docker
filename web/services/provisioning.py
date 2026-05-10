"""IdP user provisioning.

When an admin approves a pending user, Vitriol can optionally push that
user into the upstream identity provider so the user gets a real
Authentik / Keycloak / etc. account alongside their Vitriol record.
This makes the first "Continue with <IdP>" click work without the
operator having to pre-create the same person in two places.

Strategy is per-OIDC-provider, selected by ``OidcProvider.provision_kind``:

  - ``none``: do nothing. Default for back-compat — operators have to
    opt in per provider via ``provision_on_approve``.
  - ``authentik``: POST ``{username, email, name, is_active: true}``
    to ``<issuer-host>/api/v3/core/users/`` with a Bearer token from
    ``provision_api_token_enc``. The issuer URL doubles as the API
    root since Authentik colocates them on the same host.

Future kinds (``keycloak``, ``scim``) plug into the same dispatch
without schema changes.

All calls have a 15s budget — same cap as SMTP / notifications — so a
slow IdP can't hang admin actions.
"""
from __future__ import annotations
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from ..auth.crypto import decrypt
from ..models import OidcProvider, User

logger = logging.getLogger("vitriol.provisioning")

_HTTP_TIMEOUT = 15.0


def _authentik_api_root(issuer_url: str) -> str:
    """Authentik's REST API lives at ``<scheme>://<host>/api/v3/`` — same
    host as the OIDC issuer, different path. Strip the issuer down to
    its scheme+host and append the API root."""
    parsed = urlparse(issuer_url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"Issuer URL not parseable: {issuer_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}/api/v3"


async def _provision_authentik(provider: OidcProvider, user: User) -> dict:
    """Create a user record in Authentik via its admin API. Returns the
    parsed response body (Authentik returns the created user object).
    Raises on non-2xx so the caller can surface an actionable error."""
    api_root = _authentik_api_root(provider.issuer)
    token = decrypt(provider.provision_api_token_enc)
    if not token:
        raise RuntimeError(
            "Authentik API token is empty or unreadable — re-paste it in "
            "the OIDC provider edit form, then try again."
        )
    payload = {
        "username": user.username,
        "name": user.username,  # Authentik requires a display name; reuse username.
        "email": user.email or "",
        "is_active": True,
        # No password set — operator should configure Authentik's
        # password-recovery flow so the new user gets an email link to
        # set their own. Alternatively the admin can set a temporary
        # password via the Authentik UI afterward.
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(f"{api_root}/core/users/", json=payload, headers=headers)
        if r.status_code == 400:
            # Authentik's 400 body is JSON with field-level error
            # messages — surface them verbatim so the operator sees
            # what's actually wrong (duplicate username, invalid email
            # format, etc.).
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:500]
            raise RuntimeError(f"Authentik rejected user creation: {detail}")
        if r.status_code == 401 or r.status_code == 403:
            raise RuntimeError(
                f"Authentik API rejected the token ({r.status_code}). "
                "Verify the token has the 'admin' permission and isn't expired."
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Authentik /core/users/ returned {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else {}


async def provision_user_to_provider(
    db: Session, provider: OidcProvider, user: User,
) -> tuple[bool, Optional[str]]:
    """Dispatch to the right per-kind handler. Returns (ok, error)."""
    if not bool(provider.provision_on_approve):
        return True, None  # opted out for this provider, treat as success
    if not bool(provider.enabled):
        return True, None  # disabled providers shouldn't get writes either
    kind = provider.provision_kind or "none"
    if kind == "none":
        return True, None
    try:
        if kind == "authentik":
            await _provision_authentik(provider, user)
        else:
            return False, f"Unknown provision_kind {kind!r}"
    except Exception as e:
        logger.exception(
            "Provisioning to provider %s (kind=%s) failed for user %s",
            provider.slug, kind, user.username,
        )
        return False, f"{type(e).__name__}: {e}"
    return True, None


async def provision_user_to_all_enabled(db: Session, user: User) -> list[tuple[str, bool, Optional[str]]]:
    """Fan out across every enabled OIDC provider that has
    ``provision_on_approve = True``. Returns one (slug, ok, error) tuple
    per provider attempted. Failures don't block one another — admin
    sees a per-provider report after the action completes.
    """
    rows = (
        db.query(OidcProvider)
        .filter(OidcProvider.enabled.is_(True))
        .filter(OidcProvider.provision_on_approve.is_(True))
        .all()
    )
    results: list[tuple[str, bool, Optional[str]]] = []
    for r in rows:
        ok, err = await provision_user_to_provider(db, r, user)
        results.append((r.slug, ok, err))
    return results
