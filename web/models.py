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

    disabled_input_formats_json = Column(Text, nullable=False, default="[]")
    disabled_output_formats_json = Column(Text, nullable=False, default="[]")

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

    discord_webhook_url = Column(String(512), nullable=True)

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

    super_admin_can_self_compile = Column(Boolean, nullable=False, default=True)
    admin_can_self_compile = Column(Boolean, nullable=False, default=True)

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
