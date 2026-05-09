"""JWT access tokens + opaque refresh tokens (hashed in DB)."""
from __future__ import annotations
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt as pyjwt

from ..config import get_settings

_settings = get_settings()
_ALGO = "HS256"


def issue_access_token(user_id: int, role: str) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=_settings.access_token_minutes)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "typ": "access",
    }
    token = pyjwt.encode(payload, _settings.secret_key, algorithm=_ALGO)
    return token, _settings.access_token_minutes * 60


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return pyjwt.decode(token, _settings.secret_key, algorithms=[_ALGO])
    except pyjwt.PyJWTError:
        return None


def new_refresh_token() -> tuple[str, str, datetime]:
    """Returns (raw, hashed, expires_at)."""
    raw = secrets.token_urlsafe(48)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, h, datetime.utcnow() + timedelta(days=_settings.refresh_token_days)


def hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
