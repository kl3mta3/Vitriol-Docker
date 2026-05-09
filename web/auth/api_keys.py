"""API key issue + verify. Format: vit_<prefix>_<secret>.

Secret is generated server-side, returned exactly once on creation, and
stored only as sha256 hash. The prefix is shown in listings so users can
identify a key without exposing its secret.
"""
from __future__ import annotations
import hashlib
import secrets
from typing import Optional

from sqlalchemy.orm import Session

from ..models import APIKey


KEY_TAG = "vit"


def issue() -> tuple[str, str, str]:
    """Returns (full_key, prefix, key_hash)."""
    secret = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(4)   # 8 hex chars for visual identification
    full = f"{KEY_TAG}_{prefix}_{secret}"
    h = hashlib.sha256(full.encode("utf-8")).hexdigest()
    return full, prefix, h


def hash_key(full: str) -> str:
    return hashlib.sha256(full.encode("utf-8")).hexdigest()


def looks_like_api_key(value: str) -> bool:
    return value.startswith(f"{KEY_TAG}_")


def find_active(db: Session, full: str) -> Optional[APIKey]:
    if not looks_like_api_key(full):
        return None
    h = hash_key(full)
    row = db.query(APIKey).filter(APIKey.key_hash == h, APIKey.revoked_at.is_(None)).one_or_none()
    return row
