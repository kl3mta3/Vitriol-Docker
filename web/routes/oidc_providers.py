"""CRUD for /api/v1/server/oidc-providers — super admin only.

Each row is one OpenID Connect provider the super admin wants to expose
on the sign-in page (Authentik, Keycloak, Auth0, Okta, Zitadel, etc.).
The slug is URL-safe, unique, and used as the path fragment in the SSO
callback. Display name is the button label.

Slug rules: lowercase alphanumeric + dash, 2-32 chars, can't be
'google' or 'github' (those names are owned by the singleton OAuth
clients and would collide with the registry).
"""
from __future__ import annotations
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..auth.crypto import encrypt
from ..deps import get_db, require_super_admin
from ..models import OidcProvider, User
from ..services import audit

router = APIRouter(prefix="/server/oidc-providers", tags=["oidc"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
_RESERVED_SLUGS = {"google", "github", "providers"}


class OidcProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    slug: str
    display_name: str
    issuer: str
    client_id: str
    client_secret_set: bool
    scopes: str
    enabled: bool


class OidcProviderCreate(BaseModel):
    slug: Optional[str] = None         # auto-derived from display_name if blank
    display_name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str = "openid email profile"
    enabled: bool = True


class OidcProviderUpdate(BaseModel):
    slug: Optional[str] = None
    display_name: Optional[str] = None
    issuer: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None  # blank = unchanged
    scopes: Optional[str] = None
    enabled: Optional[bool] = None


def _to_out(r: OidcProvider) -> OidcProviderOut:
    return OidcProviderOut(
        id=r.id, slug=r.slug, display_name=r.display_name, issuer=r.issuer,
        client_id=r.client_id, client_secret_set=bool(r.client_secret_enc),
        scopes=r.scopes or "openid email profile", enabled=r.enabled,
    )


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    s = re.sub(r"-+", "-", s)
    return s[:32].strip("-") or "oidc"


def _validate_slug(slug: str, *, db: Session, current_id: Optional[int] = None) -> None:
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="Slug must be 3-32 chars, lowercase alphanumeric + dashes (no leading/trailing dash).",
        )
    if slug in _RESERVED_SLUGS:
        raise HTTPException(status_code=400, detail=f"Slug {slug!r} is reserved.")
    q = db.query(OidcProvider).filter(OidcProvider.slug == slug)
    if current_id is not None:
        q = q.filter(OidcProvider.id != current_id)
    if q.count() > 0:
        raise HTTPException(status_code=409, detail=f"Slug {slug!r} is already in use.")


@router.get("", response_model=List[OidcProviderOut])
def list_oidc(actor: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    return [_to_out(r) for r in db.query(OidcProvider).order_by(OidcProvider.display_name).all()]


@router.post("", response_model=OidcProviderOut)
def create_oidc(
    req: OidcProviderCreate,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    slug = (req.slug or "").strip() or _slugify(req.display_name)
    _validate_slug(slug, db=db)
    if not req.client_secret:
        raise HTTPException(status_code=400, detail="Client secret is required for new providers.")
    row = OidcProvider(
        slug=slug,
        display_name=req.display_name,
        issuer=req.issuer.strip().rstrip("/"),
        client_id=req.client_id,
        client_secret_enc=encrypt(req.client_secret),
        scopes=req.scopes or "openid email profile",
        enabled=req.enabled,
        created_by_user_id=actor.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    audit.log(db, actor.id, "oidc_provider_create", metadata={"slug": slug})
    return _to_out(row)


@router.patch("/{prov_id}", response_model=OidcProviderOut)
def update_oidc(
    prov_id: int,
    req: OidcProviderUpdate,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row: Optional[OidcProvider] = db.query(OidcProvider).get(prov_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    if req.slug is not None and req.slug != row.slug:
        _validate_slug(req.slug, db=db, current_id=row.id)
        row.slug = req.slug
    if req.display_name is not None:
        row.display_name = req.display_name
    if req.issuer is not None:
        row.issuer = req.issuer.strip().rstrip("/")
    if req.client_id is not None:
        row.client_id = req.client_id
    if req.client_secret is not None and req.client_secret != "":
        row.client_secret_enc = encrypt(req.client_secret)
    if req.scopes is not None:
        row.scopes = req.scopes or "openid email profile"
    if req.enabled is not None:
        row.enabled = req.enabled

    db.commit()
    db.refresh(row)
    audit.log(db, actor.id, "oidc_provider_update", metadata={"id": row.id, "slug": row.slug})
    return _to_out(row)


@router.delete("/{prov_id}")
def delete_oidc(
    prov_id: int,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row: Optional[OidcProvider] = db.query(OidcProvider).get(prov_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    db.delete(row)
    db.commit()
    audit.log(db, actor.id, "oidc_provider_delete", metadata={"slug": row.slug})
    return {"message": f"Provider {row.slug!r} deleted."}
