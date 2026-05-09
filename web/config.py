"""Pydantic settings — environment-driven configuration for the web app.

Anything in this module is operator-set (env vars, .env file). User-facing
runtime settings (rate limits, allow_signup, SMTP creds, etc.) live in the
`server_settings` table and are edited by the super admin in the UI.

Secret key resolution:
  1. /data/.secret_key file (highest priority — persists rotations)
  2. VITRIOL_SECRET_KEY env var (used to seed the file on first boot)
  3. Auto-generated and persisted to /data/.secret_key

This means a fresh `docker compose up` "just works" without any env config,
and the super admin can rotate the key from the UI without touching env files.
"""
from __future__ import annotations
import os
import secrets as _secrets
from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="VITRIOL_",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Core ---------------------------------------------------------
    # Raw env value — may be empty / placeholder. The actual value used by
    # the app is resolved in `resolve_secret_key()` below.
    secret_key: str = Field(
        default="",
        description="Optional override for VITRIOL_SECRET_KEY. Empty = auto-generate and persist.",
    )
    data_dir: Path = Path("/data")
    database_url: str = ""  # default computed in __init__ from data_dir

    # --- Bootstrap super admin (first run only) -----------------------
    superadmin_username: str = "superadmin"
    superadmin_password: str = ""           # required on first run
    superadmin_email: str = ""

    # --- JWT ----------------------------------------------------------
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    # --- Conversion runtime -------------------------------------------
    max_concurrent_conversions: int = 3
    upload_chunk_size: int = 1024 * 1024  # 1 MB

    # --- TLS reverse-proxy hint ---------------------------------------
    cert_dir: Path = Path("/data/certs")

    def database_url_resolved(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'vitriol.db'}"

    @property
    def upload_dir(self) -> Path:
        p = self.data_dir / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def output_dir(self) -> Path:
        p = self.data_dir / "outputs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def server_config_recovery_file(self) -> Path:
        return self.data_dir / "server_config.json"

    @property
    def secret_key_file(self) -> Path:
        return self.data_dir / ".secret_key"

    def resolve_secret_key(self) -> str:
        """Pick the right secret key, persisting to disk if needed.

        Called once during app init. The result populates `self.secret_key`
        in-place so all downstream code (jwt, crypto) keeps reading the
        same field.
        """
        f = self.secret_key_file
        # 1. Persisted file wins — that's how rotation survives restarts.
        if f.exists():
            try:
                k = f.read_text(encoding="utf-8").strip()
                if k:
                    return k
            except OSError:
                pass
        # 2. Env override — used to seed the file on first boot for
        #    pre-baked deployments.
        env_val = (self.secret_key or "").strip()
        if env_val and env_val != "replace-me-with-a-long-random-string":
            _write_secret_key(f, env_val)
            return env_val
        # 3. Auto-generate. 64 url-safe bytes = ~86 chars; plenty for HS256
        #    + PBKDF2.
        new_key = _secrets.token_urlsafe(64)
        _write_secret_key(f, new_key)
        return new_key


def _write_secret_key(path: Path, key: str) -> None:
    """Atomic write with 0600 permissions (best-effort on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(key, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    # Resolve the secret key (file → env → generate) and write it back to
    # the field so the rest of the app reads a consistent value.
    s.secret_key = s.resolve_secret_key()
    # Bridge to engine path overrides so engine code (paths.py) writes
    # everything under the same data dir.
    os.environ.setdefault("UC_USER_DATA_DIR", str(s.data_dir))
    os.environ.setdefault("UC_DOCS_DIR", str(s.output_dir))
    return s
