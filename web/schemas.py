"""Pydantic request/response schemas for the API."""
from __future__ import annotations
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field, ConfigDict


# ---------------------------------------------------------------- auth

class SignInRequest(BaseModel):
    identifier: str         # username or email
    password: str


class SignUpRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    # Optional real-name fields. Captured if the operator wants them
    # on the signup form (the template renders them but doesn't
    # require them, so signup stays low-friction).
    first_name: Optional[str] = Field(default=None, max_length=128)
    last_name: Optional[str] = Field(default=None, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class PasswordChangeRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str = Field(min_length=8, max_length=200)


class PasswordResetRequest(BaseModel):
    identifier: str


class PasswordResetConfirmRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=200)


# ---------------------------------------------------------------- users

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str]
    email_verified_at: Optional[datetime]
    role: str
    status: str
    suspended_until: Optional[datetime]
    suspension_reason: Optional[str]
    stone_enabled: bool
    self_compile_enabled: bool
    daily_conversion_limit: Optional[int]
    rate_limit_per_minute: Optional[int]
    max_file_size_bytes: Optional[int] = None    # per-user upload cap override
    max_output_size_bytes: Optional[int] = None  # per-user output-size cap override
    max_storage_bytes: Optional[int] = None      # per-user total storage quota
    # Per-user retention override. Same shape as the per-role objects
    # under server_settings.output_retention_json:
    #   {"max_files", "max_age", "age_unit", "delete_on_download"}.
    output_retention: Optional[dict] = None
    theme: str = "default"
    custom_role_id: Optional[int] = None
    custom_role_name: Optional[str] = None
    # Surfaced as a boolean rather than the hash itself so the UI can
    # decide whether to show "Change password" or "Set password" without
    # ever seeing the actual credential material.
    has_password: bool = False
    created_at: datetime
    last_login_at: Optional[datetime]


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    first_name: Optional[str] = Field(default=None, max_length=128)
    last_name: Optional[str] = Field(default=None, max_length=128)
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)
    role: str = "user"


class UserUpdateRequest(BaseModel):
    email: Optional[EmailStr] = None
    # Empty string clears the saved value; null (omitted) leaves it
    # alone. Same semantics as the secret-clearing pattern on
    # ServerSettingsPatch.
    first_name: Optional[str] = Field(default=None, max_length=128)
    last_name: Optional[str] = Field(default=None, max_length=128)
    role: Optional[str] = None
    custom_role_id: Optional[int] = None  # 0 / null = clear, positive = assign
    stone_enabled: Optional[bool] = None
    self_compile_enabled: Optional[bool] = None
    daily_conversion_limit: Optional[int] = None
    rate_limit_per_minute: Optional[int] = None
    # Per-user max upload size override. 0/null clears the override
    # (falls back to custom-role → server default). Only honored when
    # the actor has the `set_user_file_size_cap` capability; otherwise
    # the field is silently dropped from the patch.
    max_file_size_bytes: Optional[int] = None
    # Per-user max-output-size + total-storage overrides. Same gate as
    # max_file_size_bytes (reuses the `set_user_file_size_cap`
    # capability — there's no separate "storage cap" capability since
    # operators who manage one always manage the others).
    max_output_size_bytes: Optional[int] = None
    max_storage_bytes: Optional[int] = None
    # Per-user retention override. Same shape as the per-role entries
    # under server_settings.output_retention_json. {} or null clears
    # the override. Only honored when the actor has the
    # `set_user_retention` capability; otherwise silently dropped.
    output_retention: Optional[dict] = None


class SuspendRequest(BaseModel):
    duration: str   # '24h' | '3d' | '7d' | '30d'
    reason: Optional[str] = None


class SelfUpdateRequest(BaseModel):
    username: Optional[str] = Field(default=None, min_length=3, max_length=64)
    first_name: Optional[str] = Field(default=None, max_length=128)
    last_name: Optional[str] = Field(default=None, max_length=128)
    email: Optional[EmailStr] = None
    theme: Optional[str] = Field(default=None, max_length=32)


class CredentialResetRequest(BaseModel):
    new_username: Optional[str] = None
    new_email: Optional[EmailStr] = None
    new_password: Optional[str] = Field(default=None, min_length=8, max_length=200)


# ------------------------------------------------------------- api keys

class APIKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class APIKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: Optional[datetime]
    revoked_at: Optional[datetime]


class APIKeyCreateResponse(APIKeyOut):
    secret: str   # full key, returned exactly once


# ---------------------------------------------------------------- jobs

class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    src_filename: str
    src_ext: str
    dst_ext: str
    dst_filename: str
    stone: bool
    self_compile_target: Optional[str]
    verify_round_trip: bool
    status: str
    progress: int
    bytes_in: Optional[int]
    bytes_out: Optional[int]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    error: Optional[str]
    warnings_json: Optional[str]
    created_at: datetime


# ------------------------------------------------------------- settings

class ServerSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bind_host: str
    bind_port: int
    global_rate_limit_per_minute: int
    max_file_size_bytes: int
    max_output_size_bytes: int = 0
    max_storage_bytes: int = 0
    default_user_daily_limit: int
    default_user_rate_limit: int
    default_admin_daily_limit: int
    default_admin_rate_limit: int
    disabled_input_formats_json: str
    disabled_output_formats_json: str
    disabled_admin_input_formats_json: str = "[]"
    disabled_admin_output_formats_json: str = "[]"
    disabled_user_input_formats_json: str = "[]"
    disabled_user_output_formats_json: str = "[]"
    allow_signup: bool
    signup_default_role: str
    signup_default_custom_role_id: Optional[int] = None
    require_email_verification: bool = True
    password_signin_enabled: bool = True
    smtp_enabled: bool = True
    smtp_host: Optional[str]
    smtp_port: Optional[int]
    smtp_user: Optional[str]
    smtp_from: Optional[str]
    smtp_use_tls: bool
    smtp_password_set: bool = False
    smtp_last_test_at: Optional[datetime] = None
    smtp_last_test_ok: Optional[bool] = None
    discord_webhook_url: Optional[str]
    discord_last_test_at: Optional[datetime] = None
    discord_last_test_ok: Optional[bool] = None
    oauth_google_enabled: bool = True
    oauth_google_client_id: Optional[str]
    oauth_google_secret_set: bool = False
    oauth_google_last_test_at: Optional[datetime] = None
    oauth_google_last_test_ok: Optional[bool] = None
    oauth_github_enabled: bool = True
    oauth_github_client_id: Optional[str]
    oauth_github_secret_set: bool = False
    oauth_github_last_test_at: Optional[datetime] = None
    oauth_github_last_test_ok: Optional[bool] = None
    oidc_enabled: bool = False
    oidc_display_name: Optional[str] = None
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_secret_set: bool = False
    oidc_scopes: str = "openid email profile"
    public_base_url: Optional[str]
    allowed_origin: Optional[str]
    ssl_cert_pull_webhook_url: Optional[str]
    ssl_cert_pull_webhook_secret_set: bool = False
    ssl_cert_pull_mode: str = "webhook"
    ssl_cert_pull_script: Optional[str] = None
    ssl_cert_pull_auto_days: int = 0
    ssl_cert_pull_last_run_at: Optional[datetime] = None
    ssl_cert_pull_last_status: Optional[str] = None
    ssl_cert_pull_webhook_method: str = "POST"
    ssl_cert_pull_webhook_header_name: Optional[str] = None
    ssl_cert_pull_webhook_header_value_set: bool = False
    ssl_cert_pull_response_cert_field: str = "fullchain"
    ssl_cert_pull_response_key_field: str = "privkey"
    super_admin_can_self_compile: bool
    admin_can_self_compile: bool
    output_retention: dict = {}
    # Storage backend selection + S3 config. Secret returned as a
    # `*_set` boolean so the actual value never round-trips through GET.
    storage_backend: str = "local"
    s3_endpoint_url: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_region: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key_set: bool = False
    s3_path_prefix: Optional[str] = None
    s3_force_path_style: bool = False
    s3_last_test_at: Optional[datetime] = None
    s3_last_test_ok: Optional[bool] = None
    # Performance knobs (apply on restart for max_concurrent_conversions;
    # live for streaming_safety_divisor once the runtime hook is wired).
    max_concurrent_conversions: int = 3
    streaming_safety_divisor: int = 4


class ServerSettingsPatch(BaseModel):
    bind_host: Optional[str] = None
    bind_port: Optional[int] = None
    global_rate_limit_per_minute: Optional[int] = None
    max_file_size_bytes: Optional[int] = None
    max_output_size_bytes: Optional[int] = None
    max_storage_bytes: Optional[int] = None
    default_user_daily_limit: Optional[int] = None
    default_user_rate_limit: Optional[int] = None
    default_admin_daily_limit: Optional[int] = None
    default_admin_rate_limit: Optional[int] = None
    disabled_input_formats: Optional[List[str]] = None
    disabled_output_formats: Optional[List[str]] = None
    disabled_admin_input_formats: Optional[List[str]] = None
    disabled_admin_output_formats: Optional[List[str]] = None
    disabled_user_input_formats: Optional[List[str]] = None
    disabled_user_output_formats: Optional[List[str]] = None
    allow_signup: Optional[bool] = None
    signup_default_role: Optional[str] = None
    signup_default_custom_role_id: Optional[int] = None
    require_email_verification: Optional[bool] = None
    password_signin_enabled: Optional[bool] = None
    smtp_enabled: Optional[bool] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    discord_webhook_url: Optional[str] = None
    oauth_google_enabled: Optional[bool] = None
    oauth_google_client_id: Optional[str] = None
    oauth_google_client_secret: Optional[str] = None
    oauth_github_enabled: Optional[bool] = None
    oauth_github_client_id: Optional[str] = None
    oauth_github_client_secret: Optional[str] = None
    oidc_enabled: Optional[bool] = None
    oidc_display_name: Optional[str] = None
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_scopes: Optional[str] = None
    public_base_url: Optional[str] = None
    allowed_origin: Optional[str] = None
    ssl_cert_pull_webhook_url: Optional[str] = None
    ssl_cert_pull_webhook_secret: Optional[str] = None
    ssl_cert_pull_mode: Optional[str] = None
    ssl_cert_pull_script: Optional[str] = None
    ssl_cert_pull_auto_days: Optional[int] = None
    ssl_cert_pull_webhook_method: Optional[str] = None
    ssl_cert_pull_webhook_header_name: Optional[str] = None
    ssl_cert_pull_webhook_header_value: Optional[str] = None
    ssl_cert_pull_response_cert_field: Optional[str] = None
    ssl_cert_pull_response_key_field: Optional[str] = None
    super_admin_can_self_compile: Optional[bool] = None
    admin_can_self_compile: Optional[bool] = None
    output_retention: Optional[dict] = None
    storage_backend: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_region: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None      # write-only (cleartext); persisted Fernet-encrypted
    s3_path_prefix: Optional[str] = None
    s3_force_path_style: Optional[bool] = None
    max_concurrent_conversions: Optional[int] = None
    streaming_safety_divisor: Optional[int] = None


class CustomRoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: Optional[str] = None
    base_role: str
    stone_enabled: bool
    self_compile_enabled: bool
    daily_conversion_limit: Optional[int] = None
    rate_limit_per_minute: Optional[int] = None
    can_create_user: bool
    can_suspend_user: bool
    can_ban_user: bool
    can_approve_pending: bool
    can_grant_stone: bool
    can_grant_self_compile: bool
    can_restart_server: bool
    can_view_users_tab: bool
    can_reset_other_creds: bool
    can_view_own_files: bool = False
    can_view_others_files: bool = False
    can_download_others_files: bool = False
    can_delete_others_files: bool = False
    can_set_user_file_size_cap: bool = False
    can_set_user_retention: bool = False
    # Role-level upload cap. Null means inherit from server default.
    # Per-user overrides on individual rows further override this.
    max_file_size_bytes: Optional[int] = None
    # Role-level output-size cap + total-storage quota. Same pattern.
    max_output_size_bytes: Optional[int] = None
    max_storage_bytes: Optional[int] = None
    # Role-level retention override. Empty/null means inherit from
    # server default for the base role. Per-user overrides on
    # individual rows further override this.
    output_retention: Optional[dict] = None
    created_at: datetime
    user_count: int = 0


class CustomRoleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)
    base_role: str = "user"
    stone_enabled: bool = False
    self_compile_enabled: bool = False
    daily_conversion_limit: Optional[int] = None
    rate_limit_per_minute: Optional[int] = None
    can_create_user: bool = False
    can_suspend_user: bool = False
    can_ban_user: bool = False
    can_approve_pending: bool = False
    can_grant_stone: bool = False
    can_grant_self_compile: bool = False
    can_restart_server: bool = False
    can_view_users_tab: bool = False
    can_reset_other_creds: bool = False
    can_view_own_files: bool = False
    can_view_others_files: bool = False
    can_download_others_files: bool = False
    can_delete_others_files: bool = False
    can_set_user_file_size_cap: bool = False
    can_set_user_retention: bool = False
    max_file_size_bytes: Optional[int] = None
    max_output_size_bytes: Optional[int] = None
    max_storage_bytes: Optional[int] = None
    output_retention: Optional[dict] = None


class CustomRoleUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)
    base_role: Optional[str] = None
    stone_enabled: Optional[bool] = None
    self_compile_enabled: Optional[bool] = None
    daily_conversion_limit: Optional[int] = None
    rate_limit_per_minute: Optional[int] = None
    can_create_user: Optional[bool] = None
    can_suspend_user: Optional[bool] = None
    can_ban_user: Optional[bool] = None
    can_approve_pending: Optional[bool] = None
    can_grant_stone: Optional[bool] = None
    can_grant_self_compile: Optional[bool] = None
    can_restart_server: Optional[bool] = None
    can_view_users_tab: Optional[bool] = None
    can_reset_other_creds: Optional[bool] = None
    can_view_own_files: Optional[bool] = None
    can_view_others_files: Optional[bool] = None
    can_download_others_files: Optional[bool] = None
    can_delete_others_files: Optional[bool] = None
    can_set_user_file_size_cap: Optional[bool] = None
    can_set_user_retention: Optional[bool] = None
    max_file_size_bytes: Optional[int] = None
    max_output_size_bytes: Optional[int] = None
    max_storage_bytes: Optional[int] = None
    output_retention: Optional[dict] = None


class MessageResponse(BaseModel):
    message: str


class FormatsResponse(BaseModel):
    inputs: List[str]
    outputs: List[str]
    targets_for: dict   # src_ext -> [dst_ext...]
    media_categories: dict


# ------------------------------------------------------ database providers

class DbProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    slug: str
    display_name: str
    kind: str
    # Kind-specific connection params (host, port, user, db_name, sslmode,
    # db_path, extra_args). Shape mirrors NotificationChannel.config.
    config: dict = {}
    # Secret never returned in cleartext — UI just learns whether one
    # is set so it can render "(set)" vs prompting for one.
    secret_set: bool = False
    last_test_at: Optional[datetime] = None
    last_test_ok: Optional[bool] = None
    last_init_at: Optional[datetime] = None
    is_active: bool = False              # this row's URL is in /data/active_db.url
    last_migrate_at: Optional[datetime] = None
    last_migrate_status: Optional[str] = None
    created_at: datetime


class DbMigrateStatus(BaseModel):
    """Polled by the UI while a migration is in flight. Single
    in-process state — only one migration may be running at a time."""
    state: str                            # 'idle' | 'running' | 'done' | 'failed'
    provider_id: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    current_table: Optional[str] = None
    tables_done: int = 0
    tables_total: int = 0
    rows_copied: int = 0
    rows_total: int = 0
    percent: float = 0.0
    per_table: dict = {}                  # table_name -> {copied, total}
    error: Optional[str] = None


class DbProviderCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=64)
    kind: str    # validated server-side against the known kinds
    config: dict = {}
    # Optional — write-only; never returned. Stored Fernet-encrypted.
    secret: Optional[str] = None


class DbProviderUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    kind: Optional[str] = None
    config: Optional[dict] = None
    secret: Optional[str] = None   # null = leave existing; empty string = clear
