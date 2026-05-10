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

        Resolution order (first hit wins):
          1. ``SECRET_KEY`` env var, when set to a non-default value.
             Always wins. This is the only way to get deterministic
             behaviour across container restarts on hosts where the
             ``/data`` volume isn't reliably persistent (some Coolify /
             Docker PaaS configurations rebuild the volume on redeploy,
             which silently invalidates every encrypted secret in the DB
             — SMTP password, OAuth client secrets, OIDC secrets, etc.,
             all decrypt to None and downstream services fail with auth
             errors).
          2. ``/data/.secret_key`` file — works on hosts with persistent
             volumes; survives restarts and is what the in-app
             "Regenerate" button writes to.
          3. Auto-generate and persist.

        Called once during app init. The result populates ``self.secret_key``
        in-place so all downstream code (jwt, crypto) reads a consistent
        value.
        """
        import logging as _logging
        _log = _logging.getLogger("vitriol.config")

        f = self.secret_key_file
        env_val = (self.secret_key or "").strip()
        has_explicit_env = bool(env_val) and env_val != "replace-me-with-a-long-random-string"

        # 1. Explicit env var wins. Mirror it to the file so the in-UI
        #    rotation / settings export still see the live value.
        if has_explicit_env:
            try:
                _write_secret_key(f, env_val)
            except OSError:
                pass  # read-only volume is fine — env wins regardless
            _log.info("SECRET_KEY source: env var (deterministic across restarts)")
            return env_val

        # 2. Persisted file wins next — that's how rotation survives
        #    restarts on hosts where /data is genuinely persistent.
        if f.exists():
            try:
                k = f.read_text(encoding="utf-8").strip()
                if k:
                    _log.info("SECRET_KEY source: %s (existing file)", f)
                    return k
            except OSError:
                pass

        # 3. Auto-generate. 64 url-safe bytes = ~86 chars; plenty for HS256
        #    + PBKDF2.
        # Consistency check: if the application DB exists but the secret
        # file doesn't, the volume was reset between deploys. Silently
        # generating a new key would invalidate every encrypted secret in
        # the DB — operator sees mysterious "SMTP auth failed", "Google
        # SSO 500", etc. with no obvious cause. Auto-clear the now-garbage
        # encrypted columns so the misconfiguration is visible (admin
        # sees empty SMTP password, etc.) instead of broken.
        db_path = self.data_dir / "vitriol.db"
        had_prior_data = db_path.exists() and db_path.stat().st_size > 0
        new_key = _secrets.token_urlsafe(64)
        _write_secret_key(f, new_key)
        if had_prior_data:
            _log.error(
                "VOLUME RESET DETECTED: vitriol.db exists but .secret_key "
                "was missing. A new SECRET_KEY has been generated, which "
                "means every encrypted secret in the DB (SMTP password, "
                "OAuth client secrets, OIDC client secrets, cert-pull "
                "webhook secret) is now unrecoverable. The matching "
                "columns will be auto-cleared on boot so the admin UI "
                "shows them as empty — re-enter them once and set a "
                "SECRET_KEY env var so this can't happen again."
            )
            try:
                _clear_encrypted_columns_after_reset(self)
            except Exception:
                _log.exception("Failed to clear encrypted columns after volume reset")
            return new_key

        _log.warning(
            "SECRET_KEY source: AUTO-GENERATED at %s (first run). "
            "If you see this message on every restart, your /data volume "
            "isn't persistent — set a SECRET_KEY env var in your "
            "orchestrator (Coolify) so encrypted DB secrets survive "
            "restarts.", f,
        )
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


def _clear_encrypted_columns_after_reset(settings) -> None:
    """Wipe every Fernet-encrypted column in server_settings + oidc_providers
    when the DB outlived the SECRET_KEY (volume reset). Plaintext columns
    stay — only the unrecoverable encrypted blobs get nulled, so the admin
    UI shows the affected fields as empty instead of looking saved-but-broken.

    Uses sqlite3 directly so this can run before SQLAlchemy + the rest of
    the app are wired up.
    """
    import sqlite3
    db_path = settings.data_dir / "vitriol.db"
    if not db_path.exists():
        return
    enc_columns = {
        "server_settings": [
            "smtp_password_enc",
            "oauth_google_client_secret_enc",
            "oauth_github_client_secret_enc",
            "oidc_client_secret_enc",
            "ssl_cert_pull_webhook_secret_enc",
            "ssl_cert_pull_webhook_header_value_enc",
        ],
        "oidc_providers": ["client_secret_enc"],
    }
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        for table, cols in enc_columns.items():
            try:
                cur.execute(f"SELECT name FROM pragma_table_info({table!r})")
                existing = {r[0] for r in cur.fetchall()}
            except sqlite3.OperationalError:
                continue  # table doesn't exist yet (fresh DB)
            for c in cols:
                if c in existing:
                    cur.execute(f"UPDATE {table} SET {c} = NULL")
        # Also stamp test-status flags so the UI pills reflect "untested"
        # rather than the stale green "configured" from before the reset.
        try:
            cur.execute("UPDATE server_settings SET smtp_last_test_ok = NULL, discord_last_test_ok = NULL")
        except sqlite3.OperationalError:
            pass
        conn.commit()


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
