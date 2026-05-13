"""Portable export/import of server-side configuration.

Lets a super admin snapshot the running install's settings (with at-rest
secrets decrypted), optionally protect it with a password, and restore
that snapshot on a fresh deployment with a different VITRIOL_SECRET_KEY.

Envelope format (`format: "vitriol-settings-v1"`):

  unencrypted:
    {
      "format": "vitriol-settings-v1",
      "exported_at": "2026-05-10T12:34:56Z",
      "vitriol_version": "1.1.1",
      "hash": "sha256:<hex>",     # over canonical settings JSON
      "encrypted": false,
      "settings": { ... }
    }

  encrypted (Fernet, key derived via PBKDF2-HMAC-SHA256 from password):
    {
      "format": "vitriol-settings-v1",
      "exported_at": "...",
      "vitriol_version": "1.1.1",
      "hash": "sha256:<hex>",     # over the PLAINTEXT settings JSON
      "encrypted": true,
      "kdf": "pbkdf2-sha256-200000",
      "salt": "<base64>",
      "ciphertext": "<fernet token, base64-urlsafe>"
    }

The hash always covers the *plaintext* canonical settings JSON. After
decrypt, the importer recomputes hash(plaintext) and compares — a
mismatch means either the file was tampered with OR the password was
wrong. We don't try to distinguish (no oracle).
"""
from __future__ import annotations
import base64
import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from .. import __version__
from ..auth.crypto import decrypt as _decrypt_local, encrypt as _encrypt_local
from ..models import CustomRole, OidcProvider, Role, ServerSettings


FORMAT = "vitriol-settings-v1"
KDF = "pbkdf2-sha256-200000"


# ---------------------------------------------------------------------
# Collection / application — what we export, what gets restored
# ---------------------------------------------------------------------

# ServerSettings columns we DO export. Excludes auto-generated state
# (last_test_*, updated_at, storage_uris_backfilled), the singleton id,
# and the legacy top-level oidc_* fields (superseded by oidc_providers
# table). S3 secret exported separately via _SETTINGS_SECRETS.
_SETTINGS_COLUMNS = (
    "bind_host", "bind_port",
    "global_rate_limit_per_minute", "max_file_size_bytes",
    "max_output_size_bytes", "max_storage_bytes",
    "default_user_daily_limit", "default_user_rate_limit",
    "default_admin_daily_limit", "default_admin_rate_limit",
    "disabled_input_formats_json", "disabled_output_formats_json",
    "disabled_admin_input_formats_json", "disabled_admin_output_formats_json",
    "disabled_user_input_formats_json", "disabled_user_output_formats_json",
    "allow_signup", "signup_default_role", "signup_default_custom_role_id",
    "require_email_verification", "require_name_at_signup",
    "password_reset_via_email", "password_signin_enabled",
    "smtp_enabled", "smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_use_tls",
    "discord_webhook_url",
    "oauth_google_enabled", "oauth_google_show_on_signup", "oauth_google_client_id",
    "oauth_github_enabled", "oauth_github_show_on_signup", "oauth_github_client_id",
    "public_base_url", "allowed_origin",
    "brand_title", "brand_link",
    "ssl_cert_pull_webhook_url", "ssl_cert_pull_mode", "ssl_cert_pull_script",
    "ssl_cert_pull_auto_days", "ssl_cert_pull_webhook_method",
    "ssl_cert_pull_webhook_header_name",
    "ssl_cert_pull_response_cert_field", "ssl_cert_pull_response_key_field",
    "super_admin_can_self_compile", "admin_can_self_compile",
    "output_retention_json",
    # Storage backend (non-secret S3 fields; secret via _SETTINGS_SECRETS).
    "storage_backend",
    "s3_endpoint_url", "s3_bucket", "s3_region", "s3_access_key",
    "s3_path_prefix", "s3_force_path_style",
    # Performance / queue.
    "max_concurrent_conversions", "streaming_safety_divisor",
    "queue_weight_super_admin", "queue_weight_admin", "queue_weight_user",
    # Server-level border control
    "server_show_border", "server_border_style",
    # Site-wide theme defaults
    "server_default_theme", "server_allow_user_theme",
    # Support contact
    "support_email", "support_discord_url", "support_discord_shared",
    "show_limit_hit_notice", "show_user_support_button",
)

# Encrypted-at-rest columns we decrypt for export and re-encrypt on
# import. Exported under their natural plaintext keys (without `_enc`).
_SETTINGS_SECRETS = {
    "smtp_password_enc": "smtp_password",
    "oauth_google_client_secret_enc": "oauth_google_client_secret",
    "oauth_github_client_secret_enc": "oauth_github_client_secret",
    "ssl_cert_pull_webhook_secret_enc": "ssl_cert_pull_webhook_secret",
    "ssl_cert_pull_webhook_header_value_enc": "ssl_cert_pull_webhook_header_value",
    "s3_secret_key_enc": "s3_secret_key",
}


def _canonical(obj: Any) -> bytes:
    """Stable JSON encoding for hashing — sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _hash_settings(settings: dict) -> str:
    return "sha256:" + hashlib.sha256(_canonical(settings)).hexdigest()


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Role):
        return value.value
    return value


def collect(db: Session) -> dict:
    """Snapshot server config into an export-ready dict (no envelope)."""
    s = db.query(ServerSettings).get(1)
    server: dict[str, Any] = {}
    if s is not None:
        for col in _SETTINGS_COLUMNS:
            server[col] = _serialize(getattr(s, col, None))
        for enc_col, plain_key in _SETTINGS_SECRETS.items():
            server[plain_key] = _decrypt_local(getattr(s, enc_col, None))

    oidc = []
    for p in db.query(OidcProvider).order_by(OidcProvider.display_name).all():
        oidc.append({
            "slug": p.slug,
            "display_name": p.display_name,
            "issuer": p.issuer,
            "client_id": p.client_id,
            "client_secret": _decrypt_local(p.client_secret_enc),
            "scopes": p.scopes or "openid email profile",
            "enabled": bool(p.enabled),
        })

    roles = []
    for r in db.query(CustomRole).order_by(CustomRole.name).all():
        roles.append({
            "name": r.name,
            "description": r.description,
            "base_role": r.base_role.value if hasattr(r.base_role, "value") else str(r.base_role),
            "stone_enabled": r.stone_enabled,
            "self_compile_enabled": r.self_compile_enabled,
            "daily_conversion_limit": r.daily_conversion_limit,
            "rate_limit_per_minute": r.rate_limit_per_minute,
            "can_create_user": r.can_create_user,
            "can_suspend_user": r.can_suspend_user,
            "can_ban_user": r.can_ban_user,
            "can_approve_pending": r.can_approve_pending,
            "can_grant_stone": r.can_grant_stone,
            "can_grant_self_compile": r.can_grant_self_compile,
            "can_restart_server": r.can_restart_server,
            "can_view_users_tab": r.can_view_users_tab,
            "can_reset_other_creds": r.can_reset_other_creds,
            "can_view_own_files": r.can_view_own_files,
            "can_view_others_files": r.can_view_others_files,
            "can_download_others_files": r.can_download_others_files,
            "can_delete_others_files": r.can_delete_others_files,
            "queue_weight": r.queue_weight,
        })

    return {
        "server_settings": server,
        "oidc_providers": oidc,
        "custom_roles": roles,
    }


def apply(db: Session, settings: dict) -> dict:
    """Apply an imported settings dict to the live DB. Returns a small
    summary of what was touched. Caller is responsible for confirmation
    flow — by the time this is called we assume the operator has agreed.

    Strategy:
      - server_settings: update the singleton row in place. Re-encrypt
        secrets with the LOCAL secret key. Skip unknown fields silently
        (forward-compat: a v2 export with extra fields imported into a
        v1 install ignores them rather than crashing).
      - oidc_providers: upsert by slug. Existing rows with matching
        slug get updated; new slugs are inserted; rows whose slug is
        not in the import are left alone (don't surprise-delete).
      - custom_roles: upsert by name. Same policy.
    """
    summary = {"server_settings_updated": False, "oidc_providers": 0, "custom_roles": 0}
    s = db.query(ServerSettings).get(1)
    if s is None:
        s = ServerSettings(id=1)
        db.add(s)
        db.flush()

    server_in = settings.get("server_settings") or {}
    for col in _SETTINGS_COLUMNS:
        if col in server_in:
            setattr(s, col, server_in[col])
    for enc_col, plain_key in _SETTINGS_SECRETS.items():
        if plain_key in server_in:
            v = server_in[plain_key]
            setattr(s, enc_col, _encrypt_local(v) if v else None)
    summary["server_settings_updated"] = True

    for entry in (settings.get("oidc_providers") or []):
        slug = entry.get("slug")
        if not slug:
            continue
        row = db.query(OidcProvider).filter(OidcProvider.slug == slug).one_or_none()
        if row is None:
            row = OidcProvider(slug=slug)
            db.add(row)
        row.display_name = entry.get("display_name") or slug
        row.issuer = entry.get("issuer") or ""
        row.client_id = entry.get("client_id") or ""
        secret = entry.get("client_secret")
        if secret is not None:
            row.client_secret_enc = _encrypt_local(secret) if secret else row.client_secret_enc
        row.scopes = entry.get("scopes") or "openid email profile"
        row.enabled = bool(entry.get("enabled", True))
        summary["oidc_providers"] += 1

    for entry in (settings.get("custom_roles") or []):
        name = entry.get("name")
        if not name:
            continue
        row = db.query(CustomRole).filter(CustomRole.name == name).one_or_none()
        if row is None:
            row = CustomRole(name=name)
            db.add(row)
        for k in (
            "description", "base_role",
            "stone_enabled", "self_compile_enabled",
            "daily_conversion_limit", "rate_limit_per_minute",
            "can_create_user", "can_suspend_user", "can_ban_user",
            "can_approve_pending", "can_grant_stone", "can_grant_self_compile",
            "can_restart_server", "can_view_users_tab", "can_reset_other_creds",
            "can_view_own_files", "can_view_others_files",
            "can_download_others_files", "can_delete_others_files",
            "queue_weight",
        ):
            if k in entry:
                setattr(row, k, entry[k])
        summary["custom_roles"] += 1

    db.commit()
    return summary


def summarize(settings: dict) -> dict:
    """Render a short, human-readable preview of an import payload —
    fed back to the UI before the operator confirms 'Apply'."""
    server = settings.get("server_settings") or {}
    oidc = settings.get("oidc_providers") or []
    roles = settings.get("custom_roles") or []
    return {
        "smtp_host": server.get("smtp_host") or "(none)",
        "smtp_from": server.get("smtp_from") or "(none)",
        "discord_configured": bool(server.get("discord_webhook_url")),
        "google_configured": bool(server.get("oauth_google_client_id")),
        "github_configured": bool(server.get("oauth_github_client_id")),
        "allow_signup": bool(server.get("allow_signup", False)),
        "public_base_url": server.get("public_base_url") or "(none)",
        "oidc_provider_count": len(oidc),
        "oidc_slugs": [p.get("slug") for p in oidc if p.get("slug")][:8],
        "custom_role_count": len(roles),
        "custom_role_names": [r.get("name") for r in roles if r.get("name")][:8],
    }


# ---------------------------------------------------------------------
# Encryption / envelope
# ---------------------------------------------------------------------

def _derive_key(password: str, salt: bytes) -> bytes:
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000, dklen=32)
    return base64.urlsafe_b64encode(raw)


def make_envelope(settings: dict, password: Optional[str]) -> dict:
    """Wrap a settings dict into the export envelope, optionally
    encrypting the body with a password-derived key."""
    h = _hash_settings(settings)
    env: dict[str, Any] = {
        "format": FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "vitriol_version": __version__,
        "hash": h,
    }
    if password:
        salt = secrets.token_bytes(16)
        key = _derive_key(password, salt)
        plaintext = _canonical(settings)
        ciphertext = Fernet(key).encrypt(plaintext)
        env["encrypted"] = True
        env["kdf"] = KDF
        env["salt"] = base64.b64encode(salt).decode("ascii")
        env["ciphertext"] = ciphertext.decode("ascii")
    else:
        env["encrypted"] = False
        env["settings"] = settings
    return env


def parse_envelope(env: dict, password: Optional[str]) -> dict:
    """Validate envelope, decrypt if needed, verify hash, return the
    plaintext settings dict. Raises ValueError on any failure — caller
    formats the user-facing message.
    """
    if not isinstance(env, dict):
        raise ValueError("Not a settings file.")
    if env.get("format") != FORMAT:
        raise ValueError(f"Unknown format: {env.get('format')!r}")
    expected_hash = env.get("hash")
    if not (isinstance(expected_hash, str) and expected_hash.startswith("sha256:")):
        raise ValueError("Missing or malformed hash field.")

    if env.get("encrypted"):
        if not password:
            raise NeedsPasswordError()
        if env.get("kdf") != KDF:
            raise ValueError(f"Unsupported KDF: {env.get('kdf')!r}")
        try:
            salt = base64.b64decode(env["salt"])
        except Exception:
            raise ValueError("Salt is not valid base64.")
        try:
            key = _derive_key(password, salt)
            plaintext = Fernet(key).decrypt(env["ciphertext"].encode("ascii"))
        except (InvalidToken, KeyError, ValueError):
            # Combined to avoid a password-correctness oracle.
            raise ValueError("Could not decrypt — wrong password or corrupted file.")
        try:
            settings = json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Decrypted payload is not valid JSON.")
    else:
        settings = env.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("Settings body is missing.")

    actual_hash = _hash_settings(settings)
    if actual_hash != expected_hash:
        raise ValueError("Hash mismatch — file is corrupted or was tampered with.")
    return settings


class NeedsPasswordError(Exception):
    """Raised by parse_envelope when the file is encrypted and no
    password was provided. The route layer maps this to a specific
    HTTP response so the UI can prompt for password."""
