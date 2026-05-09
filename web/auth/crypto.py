"""Fernet-based at-rest encryption for secrets stored in server_settings.

The Fernet key is derived from the resolved secret key (file → env → auto)
via PBKDF2. `make_fernet(key)` is exposed so the rotation service can
re-encrypt secrets using a *different* key during a rotation operation
without disturbing the module-level instance.
"""
from __future__ import annotations
import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


_SALT = b"vitriol-server-settings-v1"


def make_fernet(key: str) -> Fernet:
    """Build a Fernet from any secret key string."""
    derived = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), _SALT, 200_000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(derived))


_settings = get_settings()
_fernet = make_fernet(_settings.secret_key)


def encrypt(plain: Optional[str]) -> Optional[str]:
    if plain is None or plain == "":
        return None
    return _fernet.encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: Optional[str]) -> Optional[str]:
    if not ciphertext:
        return None
    try:
        return _fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None


def encrypt_with(key: str, plain: Optional[str]) -> Optional[str]:
    """Encrypt with an explicit key — used during key rotation."""
    if plain is None or plain == "":
        return None
    return make_fernet(key).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_with(key: str, ciphertext: Optional[str]) -> Optional[str]:
    """Decrypt with an explicit key — used during key rotation."""
    if not ciphertext:
        return None
    try:
        return make_fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None
