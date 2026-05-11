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
from ..models import OidcProvider, Role, ServerSettings, Status, User

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


def migrate_legacy_discord(db: Session) -> None:
    """One-time copy of ServerSettings.discord_webhook_url into a
    notification_channels row.

    Triggers when:
      - the new table is empty (first boot after the multi-channel change)
      - AND ServerSettings.discord_webhook_url has a non-empty value

    The legacy column itself is left untouched so settings export/import
    files from older releases keep round-tripping; ``services.notifications``
    no longer reads it after this migration runs.
    """
    from ..models import NotificationChannel, NotificationKind
    from ..auth.crypto import encrypt
    if db.query(NotificationChannel).count() > 0:
        return
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None or not s.discord_webhook_url:
        return
    row = NotificationChannel(
        kind=NotificationKind.discord,
        name="Discord (migrated)",
        enabled=True,
        config_json="{}",
        secret_enc=encrypt(s.discord_webhook_url),
    )
    db.add(row)
    db.commit()
    _log.info("Migrated legacy discord_webhook_url into notification_channels.")


def migrate_legacy_oidc(db: Session) -> None:
    """One-time copy of the deprecated single-OIDC fields on
    ServerSettings into a row in the new oidc_providers table.

    Triggers when:
      - the new table is empty (first boot after the multi-OIDC change)
      - AND ServerSettings has a complete legacy config (issuer +
        client_id + encrypted secret).

    The legacy slug is hard-coded to 'oidc' so any redirect URI the
    operator already registered with their IdP — pointing at
    /api/v1/auth/sso/oidc/callback — keeps working without a re-config.
    """
    if db.query(OidcProvider).count() > 0:
        return
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None:
        return
    if not (s.oidc_issuer and s.oidc_client_id and s.oidc_client_secret_enc):
        return
    row = OidcProvider(
        slug="oidc",
        display_name=s.oidc_display_name or "Continue with SSO",
        issuer=s.oidc_issuer,
        client_id=s.oidc_client_id,
        client_secret_enc=s.oidc_client_secret_enc,
        scopes=s.oidc_scopes or "openid email profile",
        enabled=bool(s.oidc_enabled),
    )
    db.add(row)
    db.commit()
    _log.info("Migrated legacy single-OIDC config into oidc_providers (slug=oidc).")


# Additive column migrations. Each entry: (table, column_name, ALTER fragment).
# Used by ensure_schema() to add columns that postdate the original
# create_all baseline without requiring the user to run alembic by hand.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("server_settings", "require_email_verification",
     "ALTER TABLE server_settings ADD COLUMN require_email_verification BOOLEAN NOT NULL DEFAULT 1"),
    # Master enable toggles for the singleton integrations — let the
    # operator pause an integration without nulling out saved credentials.
    # OIDC last-test bookkeeping — drives the per-row "last test" cell
    # and pill in the OIDC providers table.
    ("oidc_providers", "last_test_at",
     "ALTER TABLE oidc_providers ADD COLUMN last_test_at TIMESTAMP"),
    ("oidc_providers", "last_test_ok",
     "ALTER TABLE oidc_providers ADD COLUMN last_test_ok BOOLEAN"),
    # Auto-provision approved users into the IdP (Authentik only for
    # now). When ``provision_on_approve`` is True, the admin promoting
    # a pending user triggers a POST to the IdP's user-creation API
    # using ``provision_api_token`` (Authentik admin token in current
    # impl). ``provision_kind`` is a strategy selector so future
    # providers (Keycloak, Okta, SCIM) can plug in without schema churn.
    ("oidc_providers", "provision_kind",
     "ALTER TABLE oidc_providers ADD COLUMN provision_kind VARCHAR(32) NOT NULL DEFAULT 'none'"),
    ("oidc_providers", "provision_on_approve",
     "ALTER TABLE oidc_providers ADD COLUMN provision_on_approve BOOLEAN NOT NULL DEFAULT 0"),
    ("oidc_providers", "provision_api_token_enc",
     "ALTER TABLE oidc_providers ADD COLUMN provision_api_token_enc TEXT"),
    # Password sign-in master toggle — when False, /signin only offers SSO.
    ("server_settings", "password_signin_enabled",
     "ALTER TABLE server_settings ADD COLUMN password_signin_enabled BOOLEAN NOT NULL DEFAULT 1"),
    ("server_settings", "smtp_enabled",
     "ALTER TABLE server_settings ADD COLUMN smtp_enabled BOOLEAN NOT NULL DEFAULT 1"),
    ("server_settings", "oauth_google_enabled",
     "ALTER TABLE server_settings ADD COLUMN oauth_google_enabled BOOLEAN NOT NULL DEFAULT 1"),
    ("server_settings", "oauth_github_enabled",
     "ALTER TABLE server_settings ADD COLUMN oauth_github_enabled BOOLEAN NOT NULL DEFAULT 1"),
    # Last-test bookkeeping for singleton OAuth providers — same shape
    # as the per-row data on oidc_providers.
    ("server_settings", "oauth_google_last_test_at",
     "ALTER TABLE server_settings ADD COLUMN oauth_google_last_test_at TIMESTAMP"),
    ("server_settings", "oauth_google_last_test_ok",
     "ALTER TABLE server_settings ADD COLUMN oauth_google_last_test_ok BOOLEAN"),
    ("server_settings", "oauth_github_last_test_at",
     "ALTER TABLE server_settings ADD COLUMN oauth_github_last_test_at TIMESTAMP"),
    ("server_settings", "oauth_github_last_test_ok",
     "ALTER TABLE server_settings ADD COLUMN oauth_github_last_test_ok BOOLEAN"),
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
    # Output retention v1 — per-role policy stored as JSON.
    ("server_settings", "output_retention_json",
     "ALTER TABLE server_settings ADD COLUMN output_retention_json TEXT NOT NULL DEFAULT "
     "'{\"super_admin\":{\"max_files\":0,\"max_age\":0,\"age_unit\":\"days\",\"delete_on_download\":false},"
     "\"admin\":{\"max_files\":0,\"max_age\":30,\"age_unit\":\"days\",\"delete_on_download\":false},"
     "\"user\":{\"max_files\":20,\"max_age\":24,\"age_unit\":\"hours\",\"delete_on_download\":false}}'"),
    # Files-tab capabilities on custom_roles. Default off — built-in
    # roles get their defaults from _ROLE_CAPS in permissions.py;
    # custom roles must opt in explicitly.
    ("custom_roles", "can_view_own_files",
     "ALTER TABLE custom_roles ADD COLUMN can_view_own_files BOOLEAN NOT NULL DEFAULT 0"),
    ("custom_roles", "can_view_others_files",
     "ALTER TABLE custom_roles ADD COLUMN can_view_others_files BOOLEAN NOT NULL DEFAULT 0"),
    ("custom_roles", "can_download_others_files",
     "ALTER TABLE custom_roles ADD COLUMN can_download_others_files BOOLEAN NOT NULL DEFAULT 0"),
    ("custom_roles", "can_delete_others_files",
     "ALTER TABLE custom_roles ADD COLUMN can_delete_others_files BOOLEAN NOT NULL DEFAULT 0"),
    # SMTP / Discord last-test tracking — drives the "configured" pill.
    ("server_settings", "smtp_last_test_at",
     "ALTER TABLE server_settings ADD COLUMN smtp_last_test_at TIMESTAMP"),
    ("server_settings", "smtp_last_test_ok",
     "ALTER TABLE server_settings ADD COLUMN smtp_last_test_ok BOOLEAN"),
    ("server_settings", "discord_last_test_at",
     "ALTER TABLE server_settings ADD COLUMN discord_last_test_at TIMESTAMP"),
    ("server_settings", "discord_last_test_ok",
     "ALTER TABLE server_settings ADD COLUMN discord_last_test_ok BOOLEAN"),
    # Tiered format disabling.
    ("server_settings", "disabled_admin_input_formats_json",
     "ALTER TABLE server_settings ADD COLUMN disabled_admin_input_formats_json TEXT NOT NULL DEFAULT '[]'"),
    ("server_settings", "disabled_admin_output_formats_json",
     "ALTER TABLE server_settings ADD COLUMN disabled_admin_output_formats_json TEXT NOT NULL DEFAULT '[]'"),
    ("server_settings", "disabled_user_input_formats_json",
     "ALTER TABLE server_settings ADD COLUMN disabled_user_input_formats_json TEXT NOT NULL DEFAULT '[]'"),
    ("server_settings", "disabled_user_output_formats_json",
     "ALTER TABLE server_settings ADD COLUMN disabled_user_output_formats_json TEXT NOT NULL DEFAULT '[]'"),
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
                # exec_driver_sql sends SQL raw — text() treats colons as
                # named-param markers, which is wrong for DDL whose default
                # value happens to be JSON literals containing colons.
                conn.exec_driver_sql(ddl)
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
    """If /data/server_config.json contains recovery blocks, apply and
    erase them. Two supported blocks:

        {
          "super_admin_recovery": {
            "username": "newname",
            "email": "x@y.z",
            "password": "newpw"
          },
          "server_recovery": {
            "allowed_origin": null,        # null = clear; "host" = set
            "public_base_url": null
          }
        }

    The server_recovery block is the emergency hatch when the operator
    has locked themselves out via a typo'd allowed_origin (or similar).
    Drop the file in /data/, restart, and the bad value is cleared.
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

    # ---- Super admin credential reset ------------------------------------
    block = data.pop("super_admin_recovery", None)
    if block:
        sa = db.query(User).filter(User.role == Role.super_admin).one_or_none()
        if sa is None:
            _log.warning("Recovery block present but no super admin row exists; will create on bootstrap.")
        else:
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
            _log.info("Applied super_admin_recovery from server_config.json")

    # ---- Server settings reset (lockout escape hatch) --------------------
    srv = data.pop("server_recovery", None)
    if srv:
        from ..models import ServerSettings as _SS
        s = db.query(_SS).get(1)
        if s is not None:
            allowed_keys = {
                "allowed_origin", "public_base_url",
                "ssl_cert_pull_webhook_url", "ssl_cert_pull_auto_days",
            }
            for k, v in srv.items():
                if k in allowed_keys:
                    setattr(s, k, v)
                    _log.info("server_recovery: set %s = %r", k, v)
            db.commit()

    # Wipe applied recovery blocks; leave the rest of the file intact.
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        _log.error("Failed to clear recovery block: %s", e)
