"""SQLAlchemy ORM models for users, auth, jobs, server settings."""
from __future__ import annotations
import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, ForeignKey, Text,
    Enum as SAEnum, UniqueConstraint, Index, Date,
)
from sqlalchemy.orm import relationship

from .db import Base


# ---------------------------------------------------------------- enums

class Role(str, enum.Enum):
    super_admin = "super_admin"
    admin = "admin"
    user = "user"
    pending = "pending"
    viewer = "viewer"


class Status(str, enum.Enum):
    active = "active"
    suspended = "suspended"
    banned = "banned"
    # Sign-up completed but the user hasn't clicked the verification email
    # link yet. Hidden from the admin user list, blocked from sign-in,
    # auto-purged after 24h. Flips to `active` on successful verify.
    unverified = "unverified"


class NotificationKind(str, enum.Enum):
    """Outbound notification channel types. Each kind has a small per-kind
    config schema documented in `web/services/notifications.py` plus a
    matching template card in the admin UI catalog. Adding a new kind:
      1. Extend this enum.
      2. Add a `_send_<kind>` handler in services/notifications.py.
      3. Add a template in admin_server.js NOTIFICATION_TEMPLATES.
      4. Add field rendering for that kind in the per-kind add/edit form.
    """
    discord = "discord"                  # webhook URL → POST {content}
    slack = "slack"                      # webhook URL → POST {text}
    ntfy = "ntfy"                        # POST plaintext to <server>/<topic>
    gotify = "gotify"                    # POST {title,message} with app token
    telegram = "telegram"                # GET sendMessage?chat_id=&text=
    generic_webhook = "generic_webhook"  # arbitrary URL/method/headers/body
    script = "script"                    # bash script with $VITRIOL_MESSAGE
    bluesky = "bluesky"                  # AT Protocol post via app password
    # signal + twitter intentionally deferred — Signal needs signal-cli
    # infra, Twitter/X needs paid API tier + OAuth 2.0 user-context auth.


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class TokenPurpose(str, enum.Enum):
    signup = "signup"
    reset = "reset"
    change_email = "change_email"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


# --------------------------------------------------------------- models

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    email_verified_at = Column(DateTime, nullable=True)
    password_hash = Column(String(255), nullable=True)  # null = SSO-only
    role = Column(SAEnum(Role, native_enum=False), nullable=False, default=Role.user)
    status = Column(SAEnum(Status, native_enum=False), nullable=False, default=Status.active)
    suspended_until = Column(DateTime, nullable=True)
    suspension_reason = Column(String(255), nullable=True)

    # Per-user grants (override role defaults when set)
    stone_enabled = Column(Boolean, nullable=False, default=False)
    self_compile_enabled = Column(Boolean, nullable=False, default=False)
    daily_conversion_limit = Column(Integer, nullable=True)     # null = role default
    rate_limit_per_minute = Column(Integer, nullable=True)      # null = role default

    # User-facing theme preference. One of THEMES (web/static/css/theme.css).
    theme = Column(String(32), nullable=False, default="default")

    # Optional custom role overlay. When set, capability checks consult
    # the linked CustomRole (capped at its base_role's ceiling) instead
    # of the bare `role` enum. The `role` column still holds the ceiling
    # so role-based queries (e.g. "all admins") keep working.
    custom_role_id = Column(Integer, ForeignKey("custom_roles.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    last_login_at = Column(DateTime, nullable=True)

    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")
    oauth_identities = relationship("OAuthIdentity", back_populates="user", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")
    custom_role = relationship("CustomRole", foreign_keys=[custom_role_id])

    # Pydantic-friendly accessor — UserOut surfaces `custom_role_name` for
    # the UI without needing a join in the route handler.
    @property
    def custom_role_name(self):
        return self.custom_role.name if self.custom_role is not None else None

    __table_args__ = (
        # Only one super admin row may exist. Partial unique indexes are
        # supported by SQLite and Postgres.
        Index(
            "uq_one_super_admin",
            "role",
            unique=True,
            sqlite_where=(role == Role.super_admin.value),
            postgresql_where=(role == Role.super_admin.value),
        ),
    )


class OidcProvider(Base):
    """Operator-defined OpenID Connect provider.

    Multiple providers can coexist (e.g. Authentik for staff + Auth0 for
    customers). The slug becomes the URL fragment in the SSO callback —
    `/api/v1/auth/sso/<slug>/callback` — so it's URL-safe and stable
    across renames. Display name is what shows on the sign-in button.

    The legacy single-OIDC fields on `ServerSettings` are auto-migrated
    into a row here on first boot (slug=`oidc` to preserve any redirect
    URI an operator may have already registered with their IdP).
    """
    __tablename__ = "oidc_providers"

    id = Column(Integer, primary_key=True)
    slug = Column(String(32), unique=True, nullable=False, index=True)
    display_name = Column(String(64), nullable=False)
    issuer = Column(String(512), nullable=False)
    client_id = Column(String(255), nullable=False)
    client_secret_enc = Column(Text, nullable=False)
    scopes = Column(String(255), nullable=False, default="openid email profile")
    enabled = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class NotificationChannel(Base):
    """One outbound notification destination — Discord webhook, Slack
    webhook, ntfy topic, Gotify app, Telegram chat, generic webhook,
    bash script, or Bluesky post.

    Multiple channels can be active at once. ``services.notifications.notify_all``
    fans out a single message to every enabled row. The legacy
    ``ServerSettings.discord_webhook_url`` is auto-migrated into a row
    here on first boot so existing setups keep working without re-config.

    Per-kind config layout (always in ``config_json``; secrets in
    ``secret_enc``):

    - discord: config={}, secret_enc=<webhook_url>
    - slack: config={}, secret_enc=<webhook_url>
    - ntfy: config={server_url, topic, auth_kind: 'none'|'bearer'|'basic'},
            secret_enc=<token or "user:pass"> (or null)
    - gotify: config={server_url}, secret_enc=<app_token>
    - telegram: config={chat_id}, secret_enc=<bot_token>
    - generic_webhook: config={url, method, headers_json, body_template},
                       secret_enc=<bearer_token> (optional)
    - script: config={script}, secret_enc=null
    - bluesky: config={handle, server: 'https://bsky.social'},
               secret_enc=<app_password>
    """
    __tablename__ = "notification_channels"

    id = Column(Integer, primary_key=True)
    kind = Column(SAEnum(NotificationKind, native_enum=False), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")

    # Per-kind, non-secret config (URL paths, topics, chat IDs, etc.).
    # Stored as JSON so adding a new kind doesn't require a migration.
    config_json = Column(Text, nullable=False, default="{}", server_default="{}")

    # Per-kind secret material (webhook URL itself, bot token, app
    # password, etc.) — Fernet-encrypted at rest using the same key as
    # the rest of the encrypted columns.
    secret_enc = Column(Text, nullable=True)

    # Last-test bookkeeping — drives the green/red pill on each row in
    # the admin UI without forcing a probe on every page load.
    last_test_at = Column(DateTime, nullable=True)
    last_test_ok = Column(Boolean, nullable=True)
    last_test_error = Column(String(500), nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomRole(Base):
    """Operator-defined role that overlays a built-in base role.

    Custom roles are "an admin with modifications, or less" — the operator
    picks a base from {admin, user, viewer, pending}, and that base sets
    the *ceiling* on what the custom role can do. The capability flags
    below let the operator dial individual powers up (within the ceiling)
    or down. A handful of capabilities are super-admin-only invariants
    and can never be granted via a custom role: viewing/editing server
    settings, creating/deleting/banning admins, viewing the secret key.
    """
    __tablename__ = "custom_roles"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False, index=True)
    description = Column(String(255), nullable=True)
    # Ceiling. SAEnum stores the value name; super_admin is rejected at
    # the API layer.
    base_role = Column(SAEnum(Role, native_enum=False), nullable=False, default=Role.user)

    # Default per-user grants applied when the role is assigned.
    stone_enabled = Column(Boolean, nullable=False, default=False)
    self_compile_enabled = Column(Boolean, nullable=False, default=False)
    daily_conversion_limit = Column(Integer, nullable=True)
    rate_limit_per_minute = Column(Integer, nullable=True)

    # Admin-tier capabilities (only meaningful when base_role == admin).
    can_create_user = Column(Boolean, nullable=False, default=False)
    can_suspend_user = Column(Boolean, nullable=False, default=False)
    can_ban_user = Column(Boolean, nullable=False, default=False)
    can_approve_pending = Column(Boolean, nullable=False, default=False)
    can_grant_stone = Column(Boolean, nullable=False, default=False)
    can_grant_self_compile = Column(Boolean, nullable=False, default=False)
    can_restart_server = Column(Boolean, nullable=False, default=False)
    can_view_users_tab = Column(Boolean, nullable=False, default=False)
    can_reset_other_creds = Column(Boolean, nullable=False, default=False)

    # Files-tab permissions. own = the user's own outputs (the Files tab
    # itself); others = looking at / acting on other users' files.
    # Built-in roles: super_admin and admin get all four; user gets only
    # can_view_own_files; viewer/pending get nothing. Custom roles can
    # toggle within their base role's ceiling.
    can_view_own_files = Column(Boolean, nullable=False, default=False, server_default="0")
    can_view_others_files = Column(Boolean, nullable=False, default=False, server_default="0")
    can_download_others_files = Column(Boolean, nullable=False, default=False, server_default="0")
    can_delete_others_files = Column(Boolean, nullable=False, default=False, server_default="0")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    key_hash = Column(String(128), nullable=False, unique=True, index=True)
    prefix = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="api_keys")


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(32), nullable=False)   # 'google' | 'github'
    subject = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="oauth_identities")

    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_oauth_provider_subject"),)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose = Column(SAEnum(TokenPurpose, native_enum=False), nullable=False)
    new_email = Column(String(255), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_role = Column(SAEnum(Role, native_enum=False), nullable=False, default=Role.user)
    status = Column(SAEnum(ApprovalStatus, native_enum=False), nullable=False, default=ApprovalStatus.pending)
    decided_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    notification_sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Session_(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_token_hash = Column(String(128), nullable=False, unique=True, index=True)
    user_agent = Column(String(255), nullable=True)
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    src_filename = Column(String(255), nullable=False)
    src_ext = Column(String(16), nullable=False)
    dst_ext = Column(String(16), nullable=False)
    dst_filename = Column(String(255), nullable=False)
    stone = Column(Boolean, nullable=False, default=False)
    self_compile_target = Column(String(8), nullable=True)  # 'py' | 'exe' | null
    verify_round_trip = Column(Boolean, nullable=False, default=False)
    status = Column(SAEnum(JobStatus, native_enum=False), nullable=False, default=JobStatus.queued)
    bytes_in = Column(Integer, nullable=True)
    bytes_out = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    warnings_json = Column(Text, nullable=True)
    progress = Column(Integer, nullable=False, default=0)  # 0–100
    src_path = Column(String(1024), nullable=False)
    dst_path = Column(String(1024), nullable=False)
    has_password = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="jobs")


class ConversionCounter(Base):
    __tablename__ = "conversion_counters"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    count = Column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_user_date"),)


class ServerSettings(Base):
    __tablename__ = "server_settings"

    id = Column(Integer, primary_key=True)  # always 1

    bind_host = Column(String(64), nullable=False, default="0.0.0.0")
    bind_port = Column(Integer, nullable=False, default=3825)

    global_rate_limit_per_minute = Column(Integer, nullable=False, default=600)
    max_file_size_bytes = Column(Integer, nullable=False, default=1024 * 1024 * 1024)  # 1 GiB

    default_user_daily_limit = Column(Integer, nullable=False, default=50)
    default_user_rate_limit = Column(Integer, nullable=False, default=30)
    default_admin_daily_limit = Column(Integer, nullable=False, default=500)
    default_admin_rate_limit = Column(Integer, nullable=False, default=120)

    # Format-disable tiers. The original two are *globally* off — nobody,
    # including the super admin, can use those formats. The two pairs
    # below cap each role tier:
    #   admin_*  → admins and below can't use these (super admin still can)
    #   user_*   → users and below can't use these (admin + super still can)
    # Conversion gate: if src/dst is in any list that applies to the
    # caller's role, reject. Stored as JSON arrays of extension strings.
    disabled_input_formats_json = Column(Text, nullable=False, default="[]")
    disabled_output_formats_json = Column(Text, nullable=False, default="[]")
    disabled_admin_input_formats_json = Column(Text, nullable=False, default="[]", server_default="[]")
    disabled_admin_output_formats_json = Column(Text, nullable=False, default="[]", server_default="[]")
    disabled_user_input_formats_json = Column(Text, nullable=False, default="[]", server_default="[]")
    disabled_user_output_formats_json = Column(Text, nullable=False, default="[]", server_default="[]")

    allow_signup = Column(Boolean, nullable=False, default=False)
    signup_default_role = Column(SAEnum(Role, native_enum=False), nullable=False, default=Role.viewer)
    # When set, new sign-ups get this custom role overlaid on top of
    # signup_default_role's ceiling. Null = builtin enum only.
    signup_default_custom_role_id = Column(Integer, ForeignKey("custom_roles.id", ondelete="SET NULL"), nullable=True)
    # When False, signups skip the verification email entirely and are
    # marked email_verified_at=now() on creation. Useful for SMTP-less
    # demos and for closed networks where email isn't reachable.
    require_email_verification = Column(Boolean, nullable=False, default=True, server_default="1")

    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_user = Column(String(255), nullable=True)
    smtp_password_enc = Column(Text, nullable=True)
    smtp_from = Column(String(255), nullable=True)
    smtp_use_tls = Column(Boolean, nullable=False, default=True)
    # Last test-email outcome: drives the green "configured" pill on the
    # admin page so a successful test sticks across reloads. NULL = never
    # tested. last_test_ok=False = failed and we should warn.
    smtp_last_test_at = Column(DateTime, nullable=True)
    smtp_last_test_ok = Column(Boolean, nullable=True)

    discord_webhook_url = Column(String(512), nullable=True)
    discord_last_test_at = Column(DateTime, nullable=True)
    discord_last_test_ok = Column(Boolean, nullable=True)

    oauth_google_client_id = Column(String(255), nullable=True)
    oauth_google_client_secret_enc = Column(Text, nullable=True)
    oauth_github_client_id = Column(String(255), nullable=True)
    oauth_github_client_secret_enc = Column(Text, nullable=True)

    # Generic OpenID Connect (Authentik, Keycloak, Auth0, Okta, etc.).
    # Only requires the issuer URL; the .well-known/openid-configuration
    # discovery handles the rest.
    oidc_enabled = Column(Boolean, nullable=False, default=False)
    oidc_display_name = Column(String(64), nullable=True)        # "Authentik", "Sign in with SSO", ...
    oidc_issuer = Column(String(512), nullable=True)              # e.g. https://auth.example.com/application/o/vitriol/
    oidc_client_id = Column(String(255), nullable=True)
    oidc_client_secret_enc = Column(Text, nullable=True)
    oidc_scopes = Column(String(255), nullable=False, default="openid email profile")

    public_base_url = Column(String(255), nullable=True)   # used in email links
    allowed_origin = Column(String(255), nullable=True)    # Host-header lock

    ssl_cert_pull_webhook_url = Column(String(512), nullable=True)
    ssl_cert_pull_webhook_secret_enc = Column(Text, nullable=True)

    # SSL cert-pull mode + extras. Two modes are supported:
    #   - webhook : POST/GET against an HTTP endpoint (existing)
    #   - script  : run a shell script that writes fullchain.pem + privkey.pem
    #               into $VITRIOL_CERT_DIR. More flexible — handles any cert
    #               API (SphereSSL, certbot, custom) without us having to
    #               anticipate auth shape or response schema.
    ssl_cert_pull_mode = Column(String(16), nullable=False, default="webhook", server_default="webhook")
    ssl_cert_pull_script = Column(Text, nullable=True)
    # 0 = manual only; >0 = re-pull every N days via the background scheduler.
    ssl_cert_pull_auto_days = Column(Integer, nullable=False, default=0, server_default="0")
    ssl_cert_pull_last_run_at = Column(DateTime, nullable=True)
    # Short message describing the last pull's outcome for UI display.
    ssl_cert_pull_last_status = Column(String(512), nullable=True)
    # Optional static-header auth for the webhook mode (e.g. SphereSSL's
    # X-Api-Key). When set, takes precedence over HMAC body signing — they
    # rarely make sense together. Header value is encrypted at rest.
    # GET is the more common shape for cert-fetch APIs (SphereSSL, custom
    # REST CAs, acme.sh wrappers). POST stays available for the legacy
    # HMAC-signed-body Vitriol webhook example, which expects POST.
    ssl_cert_pull_webhook_method = Column(String(8), nullable=False, default="GET", server_default="GET")
    ssl_cert_pull_webhook_header_name = Column(String(64), nullable=True)
    ssl_cert_pull_webhook_header_value_enc = Column(Text, nullable=True)
    # Configurable JSON field names so we can speak SphereSSL's
    # certPem/certKey shape as easily as our own fullchain/privkey.
    ssl_cert_pull_response_cert_field = Column(String(64), nullable=False, default="fullchain", server_default="fullchain")
    ssl_cert_pull_response_key_field = Column(String(64), nullable=False, default="privkey", server_default="privkey")

    super_admin_can_self_compile = Column(Boolean, nullable=False, default=True)
    admin_can_self_compile = Column(Boolean, nullable=False, default=True)

    # Per-role retention policy for converted output files. JSON shape:
    #   { "<role>": {"max_files": int, "max_age": int, "age_unit": str,
    #                "delete_on_download": bool}, ... }
    # max_files=0 → unlimited count; max_age=0 → no time-based cleanup;
    # delete_on_download=true → file is removed right after the user's
    # first successful download. Background scheduler reads this every
    # ~30 min and prunes expired/excess outputs.
    output_retention_json = Column(
        Text,
        nullable=False,
        default=(
            '{"super_admin":{"max_files":0,"max_age":0,"age_unit":"days","delete_on_download":false},'
            '"admin":{"max_files":0,"max_age":30,"age_unit":"days","delete_on_download":false},'
            '"user":{"max_files":20,"max_age":24,"age_unit":"hours","delete_on_download":false}}'
        ),
        server_default=(
            '{"super_admin":{"max_files":0,"max_age":0,"age_unit":"days","delete_on_download":false},'
            '"admin":{"max_files":0,"max_age":30,"age_unit":"days","delete_on_download":false},'
            '"user":{"max_files":20,"max_age":24,"age_unit":"hours","delete_on_download":false}}'
        ),
    )

    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(64), nullable=False, index=True)
    target_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
