"""First-boot setup: create the singleton ServerSettings row, create the
super admin from env vars, and apply optional /data/server_config.json
recovery (lets an operator reset super admin creds by editing a file)."""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..auth.password import hash_password
from ..config import get_settings
from ..models import Role, ServerSettings, Status, User

_log = logging.getLogger(__name__)


def ensure_server_settings(db: Session) -> ServerSettings:
    s = db.query(ServerSettings).get(1)
    if s is None:
        s = ServerSettings(id=1)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def super_admin_exists(db: Session) -> bool:
    return db.query(User).filter(User.role == Role.super_admin).count() > 0


# Additive column migrations. Each entry: (table, column_name, ALTER fragment).
# Used by ensure_schema() to add columns that postdate the original
# create_all baseline without requiring the user to run alembic by hand.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("server_settings", "require_email_verification",
     "ALTER TABLE server_settings ADD COLUMN require_email_verification BOOLEAN NOT NULL DEFAULT 1"),
    ("server_settings", "oidc_enabled",
     "ALTER TABLE server_settings ADD COLUMN oidc_enabled BOOLEAN NOT NULL DEFAULT 0"),
    ("server_settings", "oidc_display_name",
     "ALTER TABLE server_settings ADD COLUMN oidc_display_name VARCHAR(64)"),
    ("server_settings", "oidc_issuer",
     "ALTER TABLE server_settings ADD COLUMN oidc_issuer VARCHAR(512)"),
    ("server_settings", "oidc_client_id",
     "ALTER TABLE server_settings ADD COLUMN oidc_client_id VARCHAR(255)"),
    ("server_settings", "oidc_client_secret_enc",
     "ALTER TABLE server_settings ADD COLUMN oidc_client_secret_enc TEXT"),
    ("server_settings", "oidc_scopes",
     "ALTER TABLE server_settings ADD COLUMN oidc_scopes VARCHAR(255) NOT NULL DEFAULT 'openid email profile'"),
    ("users", "theme",
     "ALTER TABLE users ADD COLUMN theme VARCHAR(32) NOT NULL DEFAULT 'default'"),
    # Custom-role overlay — column on users + signup pointer on server_settings.
    # The custom_roles table itself is created by Base.metadata.create_all
    # at startup when missing, so no ALTER for the table is needed.
    ("users", "custom_role_id",
     "ALTER TABLE users ADD COLUMN custom_role_id INTEGER REFERENCES custom_roles(id) ON DELETE SET NULL"),
    ("server_settings", "signup_default_custom_role_id",
     "ALTER TABLE server_settings ADD COLUMN signup_default_custom_role_id INTEGER REFERENCES custom_roles(id) ON DELETE SET NULL"),
    # SSL cert-pull v2: script mode + auto-renewal + flexible webhook auth.
    ("server_settings", "ssl_cert_pull_mode",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_mode VARCHAR(16) NOT NULL DEFAULT 'webhook'"),
    ("server_settings", "ssl_cert_pull_script",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_script TEXT"),
    ("server_settings", "ssl_cert_pull_auto_days",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_auto_days INTEGER NOT NULL DEFAULT 0"),
    ("server_settings", "ssl_cert_pull_last_run_at",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_last_run_at TIMESTAMP"),
    ("server_settings", "ssl_cert_pull_last_status",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_last_status VARCHAR(512)"),
    ("server_settings", "ssl_cert_pull_webhook_method",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_webhook_method VARCHAR(8) NOT NULL DEFAULT 'POST'"),
    ("server_settings", "ssl_cert_pull_webhook_header_name",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_webhook_header_name VARCHAR(64)"),
    ("server_settings", "ssl_cert_pull_webhook_header_value_enc",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_webhook_header_value_enc TEXT"),
    ("server_settings", "ssl_cert_pull_response_cert_field",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_response_cert_field VARCHAR(64) NOT NULL DEFAULT 'fullchain'"),
    ("server_settings", "ssl_cert_pull_response_key_field",
     "ALTER TABLE server_settings ADD COLUMN ssl_cert_pull_response_key_field VARCHAR(64) NOT NULL DEFAULT 'privkey'"),
]


def ensure_schema(engine: Engine) -> None:
    """Apply additive column migrations idempotently.

    Cheap (one introspection round-trip per boot) and safe to call on every
    start. Runs *before* anything reads from the affected tables. We bias
    toward ALTER TABLE statements that work on both SQLite and Postgres —
    no type-narrowing, no NOT NULL without DEFAULT.
    """
    insp = inspect(engine)
    if not insp.get_table_names():
        return  # fresh DB; create_all() will produce the right schema
    with engine.begin() as conn:
        for table, column, ddl in _ADDITIVE_COLUMNS:
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if column in existing:
                continue
            try:
                conn.execute(text(ddl))
                _log.info("ensure_schema: added %s.%s", table, column)
            except Exception as e:
                _log.warning("ensure_schema: failed to add %s.%s: %s", table, column, e)


def ensure_super_admin(db: Session) -> Optional[User]:
    """Create the super admin from env vars, only if both are set.

    The friendly path is the first-run /setup wizard — see web/routes/setup.py.
    Env-based bootstrap stays as an escape hatch for fully-automated
    deployments (CI, infra-as-code, etc.).
    """
    cfg = get_settings()
    existing = db.query(User).filter(User.role == Role.super_admin).one_or_none()
    if existing is not None:
        return existing
    if not cfg.superadmin_password:
        _log.info("No super admin yet — open the app in a browser and the "
                  "first-run setup page will let you create one.")
        return None
    user = User(
        username=cfg.superadmin_username or "superadmin",
        email=cfg.superadmin_email or None,
        password_hash=hash_password(cfg.superadmin_password),
        role=Role.super_admin,
        status=Status.active,
        stone_enabled=True,
        self_compile_enabled=True,
        email_verified_at=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _log.info("Bootstrapped super admin '%s'", user.username)
    return user


def apply_recovery_config(db: Session) -> None:
    """If /data/server_config.json contains a super_admin_recovery block,
    apply it and delete the block. Format:

        {
          "super_admin_recovery": {
            "username": "newname",
            "email": "x@y.z",
            "password": "newpw"
          }
        }
    """
    cfg = get_settings()
    path: Path = cfg.server_config_recovery_file
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _log.error("server_config.json is not valid JSON: %s", e)
        return
    block = data.pop("super_admin_recovery", None)
    if not block:
        return
    sa = db.query(User).filter(User.role == Role.super_admin).one_or_none()
    if sa is None:
        _log.warning("Recovery block present but no super admin row exists; will create on bootstrap.")
        return
    if "username" in block and block["username"]:
        sa.username = block["username"]
    if "email" in block:
        sa.email = block["email"] or None
    if "password" in block and block["password"]:
        sa.password_hash = hash_password(block["password"])
    sa.status = Status.active
    sa.suspended_until = None
    sa.suspension_reason = None
    db.commit()
    # Wipe the recovery block; leave the rest of the file intact.
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        _log.error("Failed to clear recovery block: %s", e)
    _log.info("Applied super_admin_recovery from server_config.json")
