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
    theme: str = "default"
    custom_role_id: Optional[int] = None
    custom_role_name: Optional[str] = None
    created_at: datetime
    last_login_at: Optional[datetime]


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)
    role: str = "user"


class UserUpdateRequest(BaseModel):
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    custom_role_id: Optional[int] = None  # 0 / null = clear, positive = assign
    stone_enabled: Optional[bool] = None
    self_compile_enabled: Optional[bool] = None
    daily_conversion_limit: Optional[int] = None
    rate_limit_per_minute: Optional[int] = None


class SuspendRequest(BaseModel):
    duration: str   # '24h' | '3d' | '7d' | '30d'
    reason: Optional[str] = None


class SelfUpdateRequest(BaseModel):
    username: Optional[str] = Field(default=None, min_length=3, max_length=64)
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
    default_user_daily_limit: int
    default_user_rate_limit: int
    default_admin_daily_limit: int
    default_admin_rate_limit: int
    disabled_input_formats_json: str
    disabled_output_formats_json: str
    allow_signup: bool
    signup_default_role: str
    signup_default_custom_role_id: Optional[int] = None
    require_email_verification: bool = True
    smtp_host: Optional[str]
    smtp_port: Optional[int]
    smtp_user: Optional[str]
    smtp_from: Optional[str]
    smtp_use_tls: bool
    smtp_password_set: bool = False
    discord_webhook_url: Optional[str]
    oauth_google_client_id: Optional[str]
    oauth_google_secret_set: bool = False
    oauth_github_client_id: Optional[str]
    oauth_github_secret_set: bool = False
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
    super_admin_can_self_compile: bool
    admin_can_self_compile: bool


class ServerSettingsPatch(BaseModel):
    bind_host: Optional[str] = None
    bind_port: Optional[int] = None
    global_rate_limit_per_minute: Optional[int] = None
    max_file_size_bytes: Optional[int] = None
    default_user_daily_limit: Optional[int] = None
    default_user_rate_limit: Optional[int] = None
    default_admin_daily_limit: Optional[int] = None
    default_admin_rate_limit: Optional[int] = None
    disabled_input_formats: Optional[List[str]] = None
    disabled_output_formats: Optional[List[str]] = None
    allow_signup: Optional[bool] = None
    signup_default_role: Optional[str] = None
    signup_default_custom_role_id: Optional[int] = None
    require_email_verification: Optional[bool] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    discord_webhook_url: Optional[str] = None
    oauth_google_client_id: Optional[str] = None
    oauth_google_client_secret: Optional[str] = None
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
    super_admin_can_self_compile: Optional[bool] = None
    admin_can_self_compile: Optional[bool] = None


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


class MessageResponse(BaseModel):
    message: str


class FormatsResponse(BaseModel):
    inputs: List[str]
    outputs: List[str]
    targets_for: dict   # src_ext -> [dst_ext...]
    media_categories: dict
