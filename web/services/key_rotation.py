"""Rotate the secret key without bricking encrypted DB secrets.

The secret key serves two roles:
  - HS256 signer for JWT access tokens
  - Source for the PBKDF2-derived Fernet key used to encrypt at-rest
    secrets in `server_settings` (SMTP password, OAuth client secrets,
    SSL webhook secret).

Rotation steps (in this order — order matters for crash safety):
  1. Decrypt all encrypted fields with the OLD key (in memory).
  2. Generate a new key.
  3. Re-encrypt secrets with the NEW key (still in memory).
  4. Atomically write the new key file.
  5. Commit the re-encrypted secrets to the DB.
  6. Caller sends SIGTERM so the container restarts and reloads the new key.

If the process dies between (4) and (5) the on-disk key matches the
*old* DB secrets — they'll fail to decrypt on next start. Recovery: the
admin re-enters the secret values via the Server tab.
"""
from __future__ import annotations
import secrets as _secrets
from typing import List

from sqlalchemy.orm import Session

from ..auth.crypto import decrypt_with, encrypt_with
from ..config import _write_secret_key, get_settings
from ..models import ServerSettings


# Tuples of (column name on ServerSettings, "label for error messages")
_ENC_FIELDS: List[tuple[str, str]] = [
    ("smtp_password_enc", "SMTP password"),
    ("oauth_google_client_secret_enc", "Google OAuth secret"),
    ("oauth_github_client_secret_enc", "GitHub OAuth secret"),
    ("oidc_client_secret_enc", "OIDC client secret"),
    ("ssl_cert_pull_webhook_secret_enc", "SSL webhook secret"),
]


def rotate(db: Session) -> str:
    """Rotate the secret key, returning the new value.

    Caller is responsible for triggering a restart afterward (so the
    in-memory Fernet / JWT signer pick up the new key).
    """
    cfg = get_settings()
    old_key = cfg.secret_key

    s = db.query(ServerSettings).get(1)
    if s is None:
        raise RuntimeError("ServerSettings row missing — refusing to rotate.")

    # 1. Decrypt every encrypted field. Bail with a clear error if the
    #    current key can't read its own data — that means the file and
    #    DB are already out of sync, and rotating would only make it
    #    worse.
    plain: dict[str, str | None] = {}
    for col, label in _ENC_FIELDS:
        ciphertext = getattr(s, col)
        if not ciphertext:
            plain[col] = None
            continue
        decoded = decrypt_with(old_key, ciphertext)
        if decoded is None:
            raise RuntimeError(
                f"Cannot decrypt {label} with the current key — the secret_key "
                f"file and DB are out of sync. Re-enter that field from the "
                f"Server tab before rotating."
            )
        plain[col] = decoded

    # 2 + 3. Generate new key and re-encrypt.
    new_key = _secrets.token_urlsafe(64)
    for col, _ in _ENC_FIELDS:
        if plain[col] is None:
            continue
        setattr(s, col, encrypt_with(new_key, plain[col]))

    # 4. Persist the new key file *before* committing the DB change. If
    #    we crash between this write and the commit, the worst case is
    #    the same secrets we already had on disk — no data loss, the
    #    admin just needs to re-enter what they typed in this session.
    _write_secret_key(cfg.secret_key_file, new_key)

    # 5. Commit the re-encrypted secrets.
    db.commit()

    return new_key
