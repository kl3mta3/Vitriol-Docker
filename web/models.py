"""SQLAlchemy ORM models for users, auth, jobs, server settings."""
from __future__ import annotations
import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, ForeignKey, Text,
    Enum as SAEnum, UniqueConstraint, Index, Date, text,
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
    # Real-world name. Optional in every flow (signup, profile edit,
    # admin create). Stored as two columns rather than one "full_name"
    # so we can sort by surname, prefill the right field from an SSO
    # identity (Google + Authentik both give us split given/family),
    # and let mononymic users leave one blank. UI always displays
    # "{first} {last}".strip() so a partial value renders naturally.
    first_name = Column(String(128), nullable=True)
    last_name = Column(String(128), nullable=True)
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
    # Per-user max-file-size override. Resolution order:
    #   1. This column (per-user override) if set
    #   2. custom_role.max_file_size_bytes (per-role override) if user has one set
    #   3. server_settings.max_file_size_bytes (global default)
    # Super admin is always unlimited regardless of the chain.
    # Editing this field requires the `set_user_file_size_cap` capability.
    max_file_size_bytes = Column(Integer, nullable=True)
    # Per-user max-OUTPUT-size override. Same three-tier resolution.
    # Distinct from max_file_size_bytes because the engine can produce
    # outputs much larger than the input — a 5 MB image masqueraded into
    # a WAV can land at ~25 MB, and ZIP / video expansions are bigger.
    # Without this cap, a small upload can balloon into a disk-eating
    # output that bypasses the input limit entirely. NULL = inherit;
    # 0 = unlimited (super-admin default).
    max_output_size_bytes = Column(Integer, nullable=True)
    # Per-user total-storage quota — caps the SUM of bytes_out across
    # the user's `done` jobs whose files are still on disk. Three-tier
    # same as above. Enforced *before* an upload starts: if the user's
    # current usage already meets/exceeds this cap (or the new upload's
    # size would push over), the upload is refused with a clear message.
    # NULL = inherit; 0 = unlimited.
    max_storage_bytes = Column(Integer, nullable=True)

    # Per-user output-retention override. JSON object matching the
    # `output_retention[<role>]` shape on ServerSettings:
    #   {"max_files": int, "max_age": int, "age_unit": str, "delete_on_download": bool}
    # NULL = inherit from custom_role.output_retention_json, then from
    # the role's row in server_settings.output_retention_json.
    # Editing this field requires the `set_user_retention` capability.
    output_retention_json = Column(Text, nullable=True)

    # Per-user queue weight override for the fair conversion queue.
    # Determines how many jobs the user can burst per round-robin turn.
    # NULL = inherit the default for their role from ServerSettings.
    queue_weight_override = Column(Integer, nullable=True)

    # User-facing theme preference. One of THEMES (web/static/css/theme.css).
    theme = Column(String(32), nullable=False, default="default")
    # Per-user toggle for the alchemy SVG border around the viewport.
    # Default ON — the border is the project's signature visual. Users
    # who find it distracting or are on small screens where it's cramped
    # can flip it off in Profile. Read by base.html into a data-attribute
    # that border.js checks before rendering.
    show_border = Column(Boolean, nullable=False, default=True, server_default="1")
    # Per-user border style override. NULL = inherit server_border_style.
    # Lets individual users pick their preferred glyph / line style without
    # changing the server-wide default.
    border_style = Column(String(32), nullable=True, default=None)

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

    # Surfaced via UserOut so the profile UI can render "Set password" vs
    # "Change password" for SSO-only users without ever seeing the hash.
    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    # Parsed view of the per-user retention override JSON blob — None when
    # the column is empty or malformed. UserOut surfaces this so the admin
    # UI can render the override fields without re-parsing JSON itself.
    @property
    def output_retention(self):
        import json as _json
        if not self.output_retention_json:
            return None
        try:
            parsed = _json.loads(self.output_retention_json)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

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
    # Per-OIDC-provider signup-button gate. When False, this provider's
    # button shows on /signin but is hidden from /signup. Defaults
    # True so existing rows keep their current behaviour after upgrade.
    # Most useful for operator-private IdPs (an Authentik that gates
    # staff access, not public Reddit-visitor signups).
    show_on_signup = Column(Boolean, nullable=False, default=True, server_default="1")

    # Test bookkeeping — stamped when an operator completes a test-mode
    # SSO round-trip from /admin/server. ``last_test_ok = True`` means
    # the test popup returned a real ``sub`` from the IdP, proving the
    # client_id/secret + redirect URI + issuer URL are all consistent.
    last_test_at = Column(DateTime, nullable=True)
    last_test_ok = Column(Boolean, nullable=True)

    # Auto-provision approved users into the IdP. ``provision_kind``
    # selects the strategy ('none', 'authentik' currently — future:
    # 'keycloak', 'scim'). ``provision_on_approve`` is the per-provider
    # toggle that fires the provisioning call when an admin approves a
    # pending user. ``provision_api_token_enc`` holds the IdP-side
    # admin API token (Authentik: a long-lived service-account token).
    provision_kind = Column(String(32), nullable=False, default="none", server_default="none")
    provision_on_approve = Column(Boolean, nullable=False, default=False, server_default="0")
    provision_api_token_enc = Column(Text, nullable=True)

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
    # Role-level max upload size. Used when no per-user override is set
    # (User.max_file_size_bytes). Null = inherit from server default.
    max_file_size_bytes = Column(Integer, nullable=True)
    # Role-level max-output-size + total-storage-quota overrides. Same
    # three-tier dance as max_file_size_bytes — null means inherit from
    # the server default. Per-user values on individual rows override
    # these.
    max_output_size_bytes = Column(Integer, nullable=True)
    max_storage_bytes = Column(Integer, nullable=True)
    # Role-level output retention override (same JSON shape as the
    # per-role rows in server_settings.output_retention_json). NULL =
    # inherit from the matching built-in role default. Per-user
    # overrides on individual user rows further override this.
    output_retention_json = Column(Text, nullable=True)

    # Role-level queue weight override. Defines how many jobs users
    # with this custom role can burst per turn in the fair queue.
    # Null = inherit from the base role's server setting.
    queue_weight = Column(Integer, nullable=True)

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
    # When True, members of this role can change other users'
    # `max_file_size_bytes` per-user override (the third tier of the
    # file-size cap). Admin-tier capability — only meaningful when
    # base_role permits it (super_admin + admin get it free).
    can_set_user_file_size_cap = Column(Boolean, nullable=False, default=False, server_default="0")
    # When True, members of this role can change other users'
    # output_retention override. Admin-tier capability — base_role
    # must allow it (i.e. must equal admin).
    can_set_user_retention = Column(Boolean, nullable=False, default=False, server_default="0")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    @property
    def output_retention(self):
        import json as _json
        if not self.output_retention_json:
            return None
        try:
            parsed = _json.loads(self.output_retention_json)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None


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
    # Server-wide ceiling on a single converted output file. Stone mode
    # cross-format conversions can expand a small input into a much
    # larger output (PNG→WAV is ~3-5x, video containers can be 2-10x);
    # without this, max_file_size_bytes alone doesn't bound disk use.
    # 0 = unlimited (the default; operator opts in by setting a value).
    max_output_size_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    # Server-wide per-user total-storage quota — sum of bytes_out across
    # a user's `done` jobs whose files still exist. 0 = unlimited.
    # Enforced at upload time: if the user's current usage already
    # exceeds the quota, the upload is rejected with a "Delete or
    # Download some files to convert this" message.
    max_storage_bytes = Column(Integer, nullable=False, default=0, server_default="0")

    default_user_daily_limit = Column(Integer, nullable=False, default=50)
    default_user_rate_limit = Column(Integer, nullable=False, default=30)
    default_admin_daily_limit = Column(Integer, nullable=False, default=500)
    default_admin_rate_limit = Column(Integer, nullable=False, default=120)

    # Base queue weights for the fair round-robin conversion queue.
    # Defines how many jobs the user can burst per turn based on their role.
    queue_weight_super_admin = Column(Integer, nullable=False, default=5, server_default="5")
    queue_weight_admin = Column(Integer, nullable=False, default=3, server_default="3")
    queue_weight_user = Column(Integer, nullable=False, default=1, server_default="1")

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
    # Enforces non-empty first_name + last_name at signup. Default OFF
    # to keep the public signup flow low-friction unless the operator
    # explicitly opts in. The DB column stays nullable either way so
    # legacy rows + SSO-provisioned rows without name claims can still
    # exist. When ON: password signup rejects empty names; SSO signup
    # falls back to derived names (email-local-part, single-name copy)
    # so an Authentik/GitHub user missing the `given_name` claim still
    # gets through with *something* on file rather than a 400.
    require_name_at_signup = Column(Boolean, nullable=False, default=False, server_default="0")
    # When ON, the profile-page "Reset password" button sends an email
    # with a token-bearing link instead of opening the in-page modal
    # directly. Adds an out-of-band verification step (attacker with
    # session access can't change password without also having the
    # email inbox). Requires SMTP configured + last-tested-ok; the
    # admin UI greys the toggle out otherwise.
    password_reset_via_email = Column(Boolean, nullable=False, default=False, server_default="0")
    # Master toggle for username/password sign-in. When False, /signin's
    # form is hidden and POST /auth/signin returns 403. Useful for
    # deployments that want to enforce SSO-only access. UI side enforces
    # that at least one SSO provider is enabled before allowing this
    # to be turned off — disabling both paths would lock everyone out.
    password_signin_enabled = Column(Boolean, nullable=False, default=True, server_default="1")

    # Master enable flag — when False, all outbound SMTP is skipped even
    # if credentials are set. Lets the operator pause email without
    # clearing the saved credentials.
    smtp_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
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

    # Master enable flags for the singleton OAuth providers. Setting to
    # False hides the button from /signin and prevents the SSO callback
    # from registering the client, even when credentials are saved —
    # operator can pause an integration without wiping creds.
    oauth_google_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    # When False, the Google button appears on /signin only — not on
    # /signup. Useful when the operator wants Google as a sign-in path
    # for existing users but not as a self-service signup mechanism
    # (e.g. an internal tool gated by admin-created accounts).
    oauth_google_show_on_signup = Column(Boolean, nullable=False, default=True, server_default="1")
    oauth_google_client_id = Column(String(255), nullable=True)
    oauth_google_client_secret_enc = Column(Text, nullable=True)
    # Stamped by a successful test-mode SSO round-trip (popup → IdP →
    # /sso/google/callback) so the admin UI can show "✓ ok 2 min ago"
    # without forcing the operator to re-test on every page load.
    oauth_google_last_test_at = Column(DateTime, nullable=True)
    oauth_google_last_test_ok = Column(Boolean, nullable=True)
    oauth_github_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    # Same shape as oauth_google_show_on_signup — hides the GitHub
    # button from /signup while keeping it on /signin.
    oauth_github_show_on_signup = Column(Boolean, nullable=False, default=True, server_default="1")
    oauth_github_client_id = Column(String(255), nullable=True)
    oauth_github_client_secret_enc = Column(Text, nullable=True)
    oauth_github_last_test_at = Column(DateTime, nullable=True)
    oauth_github_last_test_ok = Column(Boolean, nullable=True)

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

    # ----------------------------------------------------- branding
    # Operator-visible brand identity. Default keeps the legacy
    # "VITRIOL → vitriol.rocks" pairing so existing deploys aren't
    # surprised by a rename. NULL falls back to the same defaults at
    # render time.
    #
    # brand_title — what shows as the nav brand text (visible top-left
    #               of every authed page).
    # brand_link  — where clicking the brand text/logo navigates.
    #               Typical use cases: operator's own marketing site,
    #               internal portal, or "javascript:void(0)" to disable
    #               the link entirely.
    brand_title = Column(String(64), nullable=True)
    brand_link = Column(String(512), nullable=True)
    # Filename of an operator-uploaded logo stored under
    # ``{data_dir}/branding/``. NULL = no custom logo uploaded, falls
    # back to the bundled /static/img/logo.svg at render time. Only the
    # base filename is stored (not the full path) so a /data path
    # rename doesn't invalidate the column. Extension determines
    # Content-Type when serving via /api/v1/server/branding/logo.
    brand_logo_filename = Column(String(255), nullable=True)
    # Server-level border controls. server_show_border=False forces the
    # border off for every user regardless of their personal preference.
    # server_border_style selects which style is rendered site-wide;
    # users cannot override the style, only their own on/off toggle
    # (which is still respected when server_show_border=True).
    server_show_border = Column(Boolean, nullable=False, default=True, server_default="1")
    server_border_style = Column(String(32), nullable=False, default="vitriol", server_default=text("'vitriol'"))
    # Site-wide theme defaults. server_default_theme sets the palette that
    # everyone sees before (or instead of) their personal choice.
    # server_allow_user_theme=False locks the UI — users can see but not
    # change the theme picker; the server_default_theme is always applied.
    server_default_theme = Column(String(32), nullable=True, default=None)
    server_allow_user_theme = Column(Boolean, nullable=False, default=True, server_default="1")

    # Support contact info surfaced to users on limit-hit notices and the
    # optional profile "Contact support" button.
    support_email = Column(String(255), nullable=True, default=None)
    support_discord_url = Column(String(512), nullable=True, default=None)
    # When True, discord link appears alongside the email in support contexts.
    support_discord_shared = Column(Boolean, nullable=False, default=False, server_default="0")
    # Show a popup when a user hits their rate/daily conversion limit.
    show_limit_hit_notice = Column(Boolean, nullable=False, default=True, server_default="1")
    # Show a "Contact support" button on the user profile page.
    show_user_support_button = Column(Boolean, nullable=False, default=False, server_default="0")

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

    # ------------------------------------------------------------- storage
    # Where converted outputs (and uploaded sources) physically live.
    # 'local' = the on-disk /data/{uploads,outputs}/ tree the app has
    # always used. 's3' = a configured S3-compatible endpoint. New writes
    # go to the active backend; existing rows are resolved by the URI
    # scheme stored on the Job row, so a switch doesn't break links to
    # files written before the flip.
    storage_backend = Column(String(16), nullable=False, default="local", server_default="local")
    s3_endpoint_url = Column(String(512), nullable=True)
    s3_bucket = Column(String(255), nullable=True)
    s3_region = Column(String(64), nullable=True)
    s3_access_key = Column(String(255), nullable=True)
    s3_secret_key_enc = Column(Text, nullable=True)
    s3_path_prefix = Column(String(255), nullable=True)
    # Path-style URLs ("https://endpoint/bucket/key") are required by
    # MinIO and some other S3 clones; virtual-hosted-style ("https://
    # bucket.endpoint/key") is the AWS default. Default off matches AWS.
    s3_force_path_style = Column(Boolean, nullable=False, default=False, server_default="0")
    s3_last_test_at = Column(DateTime, nullable=True)
    s3_last_test_ok = Column(Boolean, nullable=True)
    # One-shot flag — flipped to True the first time the boot routine
    # rewrites any bare Job.{src,dst}_path values to file:// URIs so the
    # backend resolver can dispatch by scheme uniformly. Idempotent.
    storage_uris_backfilled = Column(Boolean, nullable=False, default=False, server_default="0")

    # Tips — short lines shown on the main app page below the status bar.
    # tips_enabled=False hides the tip area entirely. Tips are stored in
    # the separate `tips` table; one is chosen at random per page load.
    tips_enabled = Column(Boolean, nullable=False, default=True, server_default="1")

    # ---------------------------------------------------------- performance
    # Worker pool size for the conversion executor. Applies at boot —
    # changing this requires a container restart so the executor can be
    # re-sized cleanly (resizing a running ThreadPoolExecutor isn't
    # supported by the stdlib).
    max_concurrent_conversions = Column(Integer, nullable=False, default=3, server_default="3")
    # Divisor on `available RAM` that produces the in-memory streaming
    # threshold (higher = more conservative = more jobs go to the
    # streaming path). 4 = the original hardcoded value; 2 lets jobs use
    # up to half of available RAM in-memory before falling back to
    # streaming. Range clamp lives in the route handler, not the column.
    streaming_safety_divisor = Column(Integer, nullable=False, default=4, server_default="4")

    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class DbProvider(Base):
    """Operator-defined alternative database connection.

    Lets the operator describe a Postgres / MySQL / MariaDB / MSSQL /
    SQLite target via the admin UI (templates pattern, same as
    NotificationChannel) so they can test the connection, optionally
    `CREATE DATABASE`, and run `Base.metadata.create_all` against an
    empty target *before* committing to a switch. Switching the live
    DB still requires the operator to restart the container after
    pointing DATABASE_URL (or the active-db file) at the new target —
    rows here are infrastructure metadata, not a live failover system.

    The actual data migration ("copy every row from old to new") is
    a deliberate next step — this table only buys you the prepared
    landing zone.
    """
    __tablename__ = "db_providers"

    id = Column(Integer, primary_key=True)
    # URL-safe identifier; stable across renames so audit log + flag
    # files keep pointing at the right row.
    slug = Column(String(32), unique=True, nullable=False, index=True)
    display_name = Column(String(64), nullable=False)
    # One of: 'sqlite' | 'postgres' | 'mysql' | 'mariadb' | 'mssql'.
    # Drives URL builder + driver selection + dialect-specific
    # CREATE DATABASE syntax in the routes layer.
    kind = Column(String(16), nullable=False)
    # JSON: kind-specific config (host, port, user, db_name, sslmode,
    # extra_args, etc.). Same shape pattern as NotificationChannel.
    config_json = Column(Text, nullable=False, default="{}", server_default="{}")
    # Fernet-encrypted password / DSN secret. NULL = no password
    # (peer auth, local SQLite, etc.).
    secret_enc = Column(Text, nullable=True)
    # Test bookkeeping — same shape as oidc_providers / s3 / SMTP.
    last_test_at = Column(DateTime, nullable=True)
    last_test_ok = Column(Boolean, nullable=True)
    # Stamped when the operator runs "Initialize schema" — gives the
    # UI a "ready to switch to" badge without re-running the DDL.
    last_init_at = Column(DateTime, nullable=True)

    # True when this provider's URL is what /data/active_db.url currently
    # contains. At most one row should have this flipped on (enforced
    # in the route layer, not the DB, because the DB has to stay
    # writable from any provider). The flag survives restart and is the
    # source of truth for which row the "active" badge shows next to.
    is_active = Column(Boolean, nullable=False, default=False, server_default="0")

    # Last migration result: snapshot of rows_copied / total_rows /
    # tables / error so the UI badge survives a page reload.
    last_migrate_at = Column(DateTime, nullable=True)
    last_migrate_status = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class Tip(Base):
    """A single tip shown randomly on the main app page below the status bar."""
    __tablename__ = "tips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(64), nullable=False, index=True)
    target_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
