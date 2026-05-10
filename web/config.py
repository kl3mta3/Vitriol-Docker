"""Pydantic settings — environment-driven configuration for the web app.

Anything in this module is operator-set (env vars, .env file). User-facing
runtime settings (rate limits, allow_signup, SMTP creds, etc.) live in the
`server_settings` table and are edited by the super admin in the UI.

Secret key resolution (first hit wins):
  1. ``SECRET_KEY`` / ``VITRIOL_SECRET_KEY`` env var, when set explicitly.
  2. ``/data/.secret_key`` file (the historical primary location).
  3. ``_key_storage`` row inside ``vitriol.db`` (a deliberate fallback —
     the application database is the most reliably-persistent thing on
     any orchestrator that ships, so storing a copy of the key there
     guarantees the encrypted columns stay readable even if the dotfile
     is wiped between restarts. Yes, this means a stolen DB file
     contains the key needed to decrypt its own secrets — encryption at
     rest becomes more of a "don't-leak-a-half-dump" property than a
     true secret-from-DB-thief property. The trade is intentional: it
     means a fresh ``docker compose up`` "just works" with zero env
     config and survives any reasonable volume hiccup.)
  4. Auto-generated and persisted to BOTH the file and the DB.

When generating a new key, both locations are written. When found in
just one location, the other is back-filled, so the two stay in sync.
"""
from __future__ import annotations
import os
import secrets as _secrets
from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic import AliasChoices, Field
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
    # the app is resolved in `resolve_secret_key()` below. Accepts both
    # ``VITRIOL_SECRET_KEY`` (prefix matches the rest of the app's env
    # vars) and the unprefixed ``SECRET_KEY`` (what most operators
    # naturally type) via the alias_choices below — either works.
    secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("VITRIOL_SECRET_KEY", "SECRET_KEY"),
        description="Optional override. Empty = auto-generate and persist to /data/.secret_key.",
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
        import hashlib as _hashlib
        _log = _logging.getLogger("vitriol.config")

        f = self.secret_key_file
        env_val = (self.secret_key or "").strip()
        has_explicit_env = bool(env_val) and env_val != "replace-me-with-a-long-random-string"

        # Boot-time filesystem snapshot. Lets the operator confirm whether
        # the volume is actually persistent by comparing this output across
        # restarts: if vitriol.db has a stable size+mtime but .secret_key
        # disappears or changes, we know something specifically targets
        # the dotfile (Coolify cache cleanup, dotfile filtering, etc.).
        # If both vanish, the volume itself is being reset. The 8-char
        # SHA prefix of the resolved key is logged at the end so two
        # successive boots can be compared without leaking the key.
        try:
            db_path = self.data_dir / "vitriol.db"
            db_stat = db_path.stat() if db_path.exists() else None
            sk_stat = f.stat() if f.exists() else None
            _log.info(
                "Filesystem probe: data_dir=%s vitriol.db=%s .secret_key=%s env_set=%s",
                self.data_dir,
                f"size={db_stat.st_size}" if db_stat else "MISSING",
                f"size={sk_stat.st_size}" if sk_stat else "MISSING",
                has_explicit_env,
            )
        except OSError as e:
            _log.warning("Filesystem probe failed: %s", e)

        def _fingerprint(k: str) -> str:
            return _hashlib.sha256(k.encode("utf-8")).hexdigest()[:8]

        # 1. Explicit env var wins. Mirror it to BOTH the file and the
        #    DB so the in-UI rotation / settings export still see the
        #    live value, and so a future boot without the env var can
        #    recover from either location.
        if has_explicit_env:
            try:
                _write_secret_key(f, env_val)
            except OSError:
                pass  # read-only volume is fine — env wins regardless
            _write_key_to_db(self, env_val)
            _log.info(
                "SECRET_KEY source: env var (fingerprint=%s) — deterministic across restarts",
                _fingerprint(env_val),
            )
            return env_val

        # 2. Persisted file. Back-fill the DB mirror so the next boot
        #    after a dotfile loss can still recover from the same DB.
        if f.exists():
            try:
                k = f.read_text(encoding="utf-8").strip()
                if k:
                    _write_key_to_db(self, k)
                    _log.info(
                        "SECRET_KEY source: %s (fingerprint=%s)",
                        f, _fingerprint(k),
                    )
                    return k
            except OSError as e:
                _log.warning("Could not read %s: %s — falling through", f, e)

        # 3. DB fallback — the file is missing but the app's SQLite DB
        #    has a persisted copy. Restore the file from the DB so future
        #    boots are fast and rotations work normally.
        db_key = _read_key_from_db(self)
        if db_key:
            try:
                _write_secret_key(f, db_key)
            except OSError as e:
                _log.warning("Recovered key from DB but could not rewrite %s: %s", f, e)
            _log.info(
                "SECRET_KEY source: vitriol.db _key_storage (fingerprint=%s) — "
                "recovered after .secret_key file loss; mirrored back to %s",
                _fingerprint(db_key), f,
            )
            return db_key

        # 4. No env, no file, no DB row — first run on a fresh volume.
        #    Generate a new key and write to both locations so the next
        #    boot can recover from either. (Previously this logged a
        #    VOLUME RESET ERROR and auto-cleared encrypted columns; now
        #    the DB fallback above handles that case transparently —
        #    encrypted columns survive a missing .secret_key file as
        #    long as the DB itself persists.)
        new_key = _secrets.token_urlsafe(64)
        try:
            _write_secret_key(f, new_key)
        except OSError as e:
            _log.warning("Could not write %s: %s — relying on DB-only storage", f, e)
        _write_key_to_db(self, new_key)
        _log.info(
            "SECRET_KEY source: AUTO-GENERATED (first run, fingerprint=%s) — "
            "persisted to %s and vitriol.db _key_storage",
            _fingerprint(new_key), f,
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


def _key_storage_db_path(settings) -> Path:
    return settings.data_dir / "vitriol.db"


def _read_key_from_db(settings) -> Optional[str]:
    """Read the persisted secret key from the `_key_storage` table inside
    the application's SQLite DB. Returns None when the DB doesn't exist
    yet (first boot) or the table/row is missing.

    Uses raw sqlite3 — this runs before SQLAlchemy + the rest of the app
    are wired up.
    """
    import sqlite3
    db_path = _key_storage_db_path(settings)
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS _key_storage ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "key TEXT NOT NULL)"
            )
            cur.execute("SELECT key FROM _key_storage WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None


def _write_key_to_db(settings, key: str) -> None:
    """Mirror the secret key into ``_key_storage`` so a missing dotfile
    on the next boot can be recovered. Best-effort — startup must not
    block on a write failure here (the file copy is still authoritative)."""
    import sqlite3
    db_path = _key_storage_db_path(settings)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS _key_storage ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "key TEXT NOT NULL)"
            )
            cur.execute(
                "INSERT INTO _key_storage (id, key) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET key = excluded.key",
                (key,),
            )
            conn.commit()
    except sqlite3.Error:
        pass  # file copy is authoritative; DB mirror is just a safety net


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
