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
    # Layered max-file-size: per-user override on users + per-role
    # override on custom_roles. Null on both means "inherit from
    # server_settings.max_file_size_bytes" (the global default).
    # Editing the per-user value is gated on the new
    # `set_user_file_size_cap` capability.
    ("users", "max_file_size_bytes",
     "ALTER TABLE users ADD COLUMN max_file_size_bytes INTEGER"),
    ("custom_roles", "max_file_size_bytes",
     "ALTER TABLE custom_roles ADD COLUMN max_file_size_bytes INTEGER"),
    ("custom_roles", "can_set_user_file_size_cap",
     "ALTER TABLE custom_roles ADD COLUMN can_set_user_file_size_cap BOOLEAN NOT NULL DEFAULT 0"),
    # Layered output-retention overrides (same three-tier pattern as
    # max_file_size_bytes). Stored as JSON to match the existing
    # per-built-in-role shape on server_settings.output_retention_json.
    ("users", "output_retention_json",
     "ALTER TABLE users ADD COLUMN output_retention_json TEXT"),
    ("custom_roles", "output_retention_json",
     "ALTER TABLE custom_roles ADD COLUMN output_retention_json TEXT"),
    ("custom_roles", "can_set_user_retention",
     "ALTER TABLE custom_roles ADD COLUMN can_set_user_retention BOOLEAN NOT NULL DEFAULT 0"),
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
    # Storage backend (local default vs operator-configured S3-compatible
    # endpoint). All s3_* columns are nullable — they only matter when
    # storage_backend='s3'. Secret is Fernet-encrypted at rest, same as
    # every other *_enc column on this table.
    ("server_settings", "storage_backend",
     "ALTER TABLE server_settings ADD COLUMN storage_backend VARCHAR(16) NOT NULL DEFAULT 'local'"),
    ("server_settings", "s3_endpoint_url",
     "ALTER TABLE server_settings ADD COLUMN s3_endpoint_url VARCHAR(512)"),
    ("server_settings", "s3_bucket",
     "ALTER TABLE server_settings ADD COLUMN s3_bucket VARCHAR(255)"),
    ("server_settings", "s3_region",
     "ALTER TABLE server_settings ADD COLUMN s3_region VARCHAR(64)"),
    ("server_settings", "s3_access_key",
     "ALTER TABLE server_settings ADD COLUMN s3_access_key VARCHAR(255)"),
    ("server_settings", "s3_secret_key_enc",
     "ALTER TABLE server_settings ADD COLUMN s3_secret_key_enc TEXT"),
    ("server_settings", "s3_path_prefix",
     "ALTER TABLE server_settings ADD COLUMN s3_path_prefix VARCHAR(255)"),
    ("server_settings", "s3_force_path_style",
     "ALTER TABLE server_settings ADD COLUMN s3_force_path_style BOOLEAN NOT NULL DEFAULT 0"),
    ("server_settings", "s3_last_test_at",
     "ALTER TABLE server_settings ADD COLUMN s3_last_test_at TIMESTAMP"),
    ("server_settings", "s3_last_test_ok",
     "ALTER TABLE server_settings ADD COLUMN s3_last_test_ok BOOLEAN"),
    ("server_settings", "storage_uris_backfilled",
     "ALTER TABLE server_settings ADD COLUMN storage_uris_backfilled BOOLEAN NOT NULL DEFAULT 0"),
    # Performance knobs. max_concurrent_conversions applies on next boot
    # (the ThreadPoolExecutor isn't resizable in the stdlib); the safety
    # divisor is read live from the DB by app/core/config via a runtime
    # override hook that the web layer wires up on settings change.
    ("server_settings", "max_concurrent_conversions",
     "ALTER TABLE server_settings ADD COLUMN max_concurrent_conversions INTEGER NOT NULL DEFAULT 3"),
    ("server_settings", "streaming_safety_divisor",
     "ALTER TABLE server_settings ADD COLUMN streaming_safety_divisor INTEGER NOT NULL DEFAULT 4"),
    # DbProvider — `is_active` marks the row whose URL is currently in
    # /data/active_db.url (at most one); `last_migrate_*` snapshots the
    # most recent row-copy result for the badge in the UI. Safe to
    # re-run if create_all already produced the table with these
    # columns — the additive check skips when the column exists.
    ("db_providers", "is_active",
     "ALTER TABLE db_providers ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 0"),
    ("db_providers", "last_migrate_at",
     "ALTER TABLE db_providers ADD COLUMN last_migrate_at TIMESTAMP"),
    ("db_providers", "last_migrate_status",
     "ALTER TABLE db_providers ADD COLUMN last_migrate_status VARCHAR(255)"),
    # Per-conversion output-size cap. Distinct from max_file_size_bytes
    # (which caps INPUT) because Stone-mode expansions can balloon a
    # small input into a much larger output. Three-tier resolution:
    # user → custom_role → server_settings. 0 = unlimited.
    ("server_settings", "max_output_size_bytes",
     "ALTER TABLE server_settings ADD COLUMN max_output_size_bytes INTEGER NOT NULL DEFAULT 0"),
    ("users", "max_output_size_bytes",
     "ALTER TABLE users ADD COLUMN max_output_size_bytes INTEGER"),
    ("custom_roles", "max_output_size_bytes",
     "ALTER TABLE custom_roles ADD COLUMN max_output_size_bytes INTEGER"),
    # Per-user total-storage quota — sum of bytes_out across done jobs.
    # Three-tier same as above. 0 = unlimited. Enforced at upload time;
    # user is told to delete or download some files to free space.
    ("server_settings", "max_storage_bytes",
     "ALTER TABLE server_settings ADD COLUMN max_storage_bytes INTEGER NOT NULL DEFAULT 0"),
    ("users", "max_storage_bytes",
     "ALTER TABLE users ADD COLUMN max_storage_bytes INTEGER"),
    # Optional real-world name fields. SSO callbacks auto-fill these
    # from the IdP's given_name / family_name claims when present;
    # otherwise the user types them in profile / admin sets at create.
    ("users", "first_name",
     "ALTER TABLE users ADD COLUMN first_name VARCHAR(128)"),
    ("users", "last_name",
     "ALTER TABLE users ADD COLUMN last_name VARCHAR(128)"),
    ("custom_roles", "max_storage_bytes",
     "ALTER TABLE custom_roles ADD COLUMN max_storage_bytes INTEGER"),
    # Per-provider "show this button on the /signup page" toggle.
    # `enabled` already controls visibility on /signin; this is the
    # second axis. Default ON for backward compat — existing operators
    # see no behaviour change unless they explicitly turn it off.
    ("server_settings", "oauth_google_show_on_signup",
     "ALTER TABLE server_settings ADD COLUMN oauth_google_show_on_signup BOOLEAN NOT NULL DEFAULT 1"),
    ("server_settings", "oauth_github_show_on_signup",
     "ALTER TABLE server_settings ADD COLUMN oauth_github_show_on_signup BOOLEAN NOT NULL DEFAULT 1"),
    ("oidc_providers", "show_on_signup",
     "ALTER TABLE oidc_providers ADD COLUMN show_on_signup BOOLEAN NOT NULL DEFAULT 1"),
    # Toggle for "name fields are required at password signup". DB
    # column stays nullable; this just gates the SignUpRequest
    # validation in the route handler. Default OFF for backward compat.
    ("server_settings", "require_name_at_signup",
     "ALTER TABLE server_settings ADD COLUMN require_name_at_signup BOOLEAN NOT NULL DEFAULT 0"),
    # Gates whether profile-page password resets use an email-link
    # verification step. Default OFF; requires SMTP configured + tested.
    ("server_settings", "password_reset_via_email",
     "ALTER TABLE server_settings ADD COLUMN password_reset_via_email BOOLEAN NOT NULL DEFAULT 0"),
    # Operator-customizable brand title + link shown in the nav. NULL =
    # fall back to the built-in "VITRIOL → vitriol.rocks" pairing at
    # render time, so existing deploys without explicit values keep the
    # same look they had before.
    ("server_settings", "brand_title",
     "ALTER TABLE server_settings ADD COLUMN brand_title VARCHAR(64)"),
    ("server_settings", "brand_link",
     "ALTER TABLE server_settings ADD COLUMN brand_link VARCHAR(512)"),
    # Operator-uploaded logo filename. File lives under
    # {data_dir}/branding/; column is NULL when no upload exists and
    # the bundled default SVG is served instead.
    ("server_settings", "brand_logo_filename",
     "ALTER TABLE server_settings ADD COLUMN brand_logo_filename VARCHAR(255)"),
    # Per-user toggle for the alchemy SVG border. Default ON so existing
    # users see the same visual they had before the column existed.
    ("users", "show_border",
     "ALTER TABLE users ADD COLUMN show_border BOOLEAN NOT NULL DEFAULT 1"),
    # Queue weights
    ("server_settings", "queue_weight_super_admin",
     "ALTER TABLE server_settings ADD COLUMN queue_weight_super_admin INTEGER NOT NULL DEFAULT 5"),
    ("server_settings", "queue_weight_admin",
     "ALTER TABLE server_settings ADD COLUMN queue_weight_admin INTEGER NOT NULL DEFAULT 3"),
    ("server_settings", "queue_weight_user",
     "ALTER TABLE server_settings ADD COLUMN queue_weight_user INTEGER NOT NULL DEFAULT 1"),
    ("custom_roles", "queue_weight",
     "ALTER TABLE custom_roles ADD COLUMN queue_weight INTEGER"),
    ("users", "queue_weight_override",
     "ALTER TABLE users ADD COLUMN queue_weight_override INTEGER"),
    # Missing custom_roles columns that were added but missed in earlier bootstraps
    ("custom_roles", "max_file_size_bytes",
     "ALTER TABLE custom_roles ADD COLUMN max_file_size_bytes INTEGER"),
    ("custom_roles", "max_output_size_bytes",
     "ALTER TABLE custom_roles ADD COLUMN max_output_size_bytes INTEGER"),
    ("custom_roles", "output_retention_json",
     "ALTER TABLE custom_roles ADD COLUMN output_retention_json TEXT"),
    ("custom_roles", "can_set_user_file_size_cap",
     "ALTER TABLE custom_roles ADD COLUMN can_set_user_file_size_cap BOOLEAN NOT NULL DEFAULT 0"),
    ("custom_roles", "can_set_user_retention",
     "ALTER TABLE custom_roles ADD COLUMN can_set_user_retention BOOLEAN NOT NULL DEFAULT 0"),
    # Server-level border control: master toggle + style selector
    ("server_settings", "server_show_border",
     "ALTER TABLE server_settings ADD COLUMN server_show_border BOOLEAN NOT NULL DEFAULT 1"),
    ("server_settings", "server_border_style",
     "ALTER TABLE server_settings ADD COLUMN server_border_style VARCHAR(32) NOT NULL DEFAULT 'vitriol'"),
    # Per-user border style override (NULL = follow server default)
    ("users", "border_style",
     "ALTER TABLE users ADD COLUMN border_style VARCHAR(32)"),
    # Server-wide theme defaults + user-theme lock
    ("server_settings", "server_default_theme",
     "ALTER TABLE server_settings ADD COLUMN server_default_theme VARCHAR(32)"),
    ("server_settings", "server_allow_user_theme",
     "ALTER TABLE server_settings ADD COLUMN server_allow_user_theme BOOLEAN NOT NULL DEFAULT 1"),
    # Support contact info + user-facing notice toggles
    ("server_settings", "support_email",
     "ALTER TABLE server_settings ADD COLUMN support_email VARCHAR(255)"),
    ("server_settings", "support_discord_url",
     "ALTER TABLE server_settings ADD COLUMN support_discord_url VARCHAR(512)"),
    ("server_settings", "support_discord_shared",
     "ALTER TABLE server_settings ADD COLUMN support_discord_shared BOOLEAN NOT NULL DEFAULT 0"),
    ("server_settings", "show_limit_hit_notice",
     "ALTER TABLE server_settings ADD COLUMN show_limit_hit_notice BOOLEAN NOT NULL DEFAULT 1"),
    ("server_settings", "show_user_support_button",
     "ALTER TABLE server_settings ADD COLUMN show_user_support_button BOOLEAN NOT NULL DEFAULT 0"),
    ("server_settings", "tips_enabled",
     "ALTER TABLE server_settings ADD COLUMN tips_enabled BOOLEAN NOT NULL DEFAULT 1"),
    # Sudo-admin flag — elevates an admin to full user-management rights
    # over other admins without granting server-settings access.
    ("users", "is_sudo_admin",
     "ALTER TABLE users ADD COLUMN is_sudo_admin BOOLEAN NOT NULL DEFAULT 0"),
    # Self-delete feature — server-wide master toggle and per-role flag.
    ("server_settings", "allow_self_delete",
     "ALTER TABLE server_settings ADD COLUMN allow_self_delete BOOLEAN NOT NULL DEFAULT 1"),
    ("custom_roles", "can_self_delete",
     "ALTER TABLE custom_roles ADD COLUMN can_self_delete BOOLEAN NOT NULL DEFAULT 1"),
    # Active-transmutes admin tab — visibility into the live queue +
    # running conversions with skip/pause/stop controls. Admin-tier
    # custom roles opt in via this flag; built-in admin + super_admin
    # get it automatically through _ROLE_CAPS in permissions.py.
    ("custom_roles", "can_view_active_transmutes",
     "ALTER TABLE custom_roles ADD COLUMN can_view_active_transmutes BOOLEAN NOT NULL DEFAULT 0"),
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

    Also enforces the super-admin row's name as "Super" + "Admin" on
    every boot. This is a *system identity*, not a personal one — it
    represents the bootstrap-root role, not the human operating it.
    If the operator wants their personal name on a row, they should
    create themselves a regular admin account via the Users tab and
    keep the super-admin row separate (best-practice anyway: don't
    do day-to-day work as the bootstrap-root account).

    The lock is unconditional — any name set via PATCH /me or /users
    on the super-admin row gets reset on the next restart.
    """
    cfg = get_settings()
    existing = db.query(User).filter(User.role == Role.super_admin).one_or_none()
    if existing is not None:
        # Hard-lock — always overwrite, regardless of current value.
        # Idempotent: if the row is already "Super"/"Admin", nothing
        # changes; commit() is harmless on a no-op session.
        changed = False
        if existing.first_name != "Super":
            existing.first_name = "Super"
            changed = True
        if existing.last_name != "Admin":
            existing.last_name = "Admin"
            changed = True
        if changed:
            db.commit()
            _log.info("ensure_super_admin: reset super admin row name to 'Super Admin' "
                      "(system identity — operator personal name should live on a "
                      "separate regular-admin account)")
        return existing
    if not cfg.superadmin_password:
        _log.info("No super admin yet — open the app in a browser and the "
                  "first-run setup page will let you create one.")
        return None
    user = User(
        username=cfg.superadmin_username or "superadmin",
        first_name="Super",
        last_name="Admin",
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


_DEFAULT_TIPS = [
    "Calculating optimal sadness codec.",
    "Transcoding your expectations.",
    "Hiding things in plain byte.",
    "Eating through the metadata.",
    "We GIF a damn about conversions.",
    "Fast conversions, MP4 real.",
    "Taking your files from RAW to ready.",
    "What's up, DOCx?",
    "What can we say? It is a GIFt.",
    "We love to TXT.",
    "Going from PNG to pro.",
    "Things are RAR-ely this easy.",
    "MOV over, We're making WAVs.",
    "EXE-cellent!",
    "XLSX marks the spot.",
    "FLAC it.",
    "Your EXE still calls us to say hello.",
    "Dude, I got some freaking file conversions at work today. Hell Yeah!",
    "What's a pirate's favorite format? It be RARrrrrrrr!",
]


def seed_default_tips(db: Session) -> None:
    """Populate the tips table with the built-in defaults on first boot.

    Does nothing if any tips already exist, so operator edits are never
    overwritten on subsequent restarts.
    """
    from ..models import Tip
    if db.query(Tip).count() > 0:
        return
    for body in _DEFAULT_TIPS:
        db.add(Tip(body=body))
    db.commit()
    _log.info("Seeded %d default tips.", len(_DEFAULT_TIPS))


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
