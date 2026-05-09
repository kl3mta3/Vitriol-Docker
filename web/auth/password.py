"""Argon2id password hashing."""
from __future__ import annotations
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHash):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHash:
        return True
