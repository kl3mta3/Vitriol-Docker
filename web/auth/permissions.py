"""Role/permission decision table — single source of truth.

Every privileged action in the API checks here. The UI also queries this
to decide which tabs to render, so the two never drift.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from ..models import Role, Status

if TYPE_CHECKING:
    from ..models import User


# --- Capability constants -----------------------------------------------

CAN_VIEW_APP = "view_app"
CAN_RUN_CONVERSION = "run_conversion"
CAN_USE_STONE = "use_stone"
CAN_USE_SELF_COMPILE = "use_self_compile"
CAN_VIEW_PROFILE = "view_profile"
CAN_VIEW_USERS_TAB = "view_users_tab"
CAN_VIEW_SERVER_TAB = "view_server_tab"
CAN_CREATE_USER = "create_user"
CAN_CREATE_ADMIN = "create_admin"
CAN_DELETE_ADMIN = "delete_admin"
CAN_RESET_OTHER_CREDS = "reset_other_creds"
CAN_RESET_ANY_CREDS = "reset_any_creds"
CAN_SUSPEND_USER = "suspend_user"
CAN_BAN_USER = "ban_user"
CAN_SUSPEND_ADMIN = "suspend_admin"
CAN_BAN_ADMIN = "ban_admin"
CAN_RESTART_SERVER = "restart_server"
CAN_EDIT_SERVER_SETTINGS = "edit_server_settings"
CAN_APPROVE_PENDING = "approve_pending"
CAN_GRANT_STONE = "grant_stone"
CAN_GRANT_SELF_COMPILE = "grant_self_compile"


# --- Per-role base capabilities ----------------------------------------

_ROLE_CAPS: dict[Role, set[str]] = {
    Role.super_admin: {
        CAN_VIEW_APP, CAN_RUN_CONVERSION, CAN_USE_STONE, CAN_USE_SELF_COMPILE,
        CAN_VIEW_PROFILE, CAN_VIEW_USERS_TAB, CAN_VIEW_SERVER_TAB,
        CAN_CREATE_USER, CAN_CREATE_ADMIN, CAN_DELETE_ADMIN,
        CAN_RESET_OTHER_CREDS, CAN_RESET_ANY_CREDS,
        CAN_SUSPEND_USER, CAN_BAN_USER, CAN_SUSPEND_ADMIN, CAN_BAN_ADMIN,
        CAN_RESTART_SERVER, CAN_EDIT_SERVER_SETTINGS,
        CAN_APPROVE_PENDING, CAN_GRANT_STONE, CAN_GRANT_SELF_COMPILE,
    },
    Role.admin: {
        CAN_VIEW_APP, CAN_RUN_CONVERSION, CAN_USE_STONE, CAN_USE_SELF_COMPILE,
        CAN_VIEW_PROFILE, CAN_VIEW_USERS_TAB,
        CAN_CREATE_USER, CAN_RESET_OTHER_CREDS,
        CAN_SUSPEND_USER, CAN_BAN_USER, CAN_SUSPEND_ADMIN,
        CAN_RESTART_SERVER,
        CAN_APPROVE_PENDING, CAN_GRANT_STONE, CAN_GRANT_SELF_COMPILE,
    },
    Role.user: {
        CAN_VIEW_APP, CAN_RUN_CONVERSION, CAN_VIEW_PROFILE,
    },
    Role.pending: set(),
    Role.viewer: {
        CAN_VIEW_APP, CAN_VIEW_PROFILE,   # read-only view; CAN_RUN_CONVERSION absent
    },
}


# Capabilities a custom role can never grant — these stay super-admin-only
# regardless of base ceiling or flag state. Defense-in-depth alongside
# the API-layer rejections in routes/roles.py.
_CUSTOM_FORBIDDEN: set[str] = {
    CAN_VIEW_SERVER_TAB,
    CAN_EDIT_SERVER_SETTINGS,
    CAN_CREATE_ADMIN,
    CAN_DELETE_ADMIN,
    CAN_BAN_ADMIN,
    CAN_SUSPEND_ADMIN,
    CAN_RESET_ANY_CREDS,
}


# Map: capability constant → CustomRole flag attribute name. Capabilities
# not listed here can't be toggled by a custom role (they either come from
# the base ceiling automatically, or are forbidden — see above).
_CUSTOM_FLAG_FOR_CAP: dict[str, str] = {
    CAN_USE_STONE: "stone_enabled",
    # Self-compile additionally requires stone (see fallthrough below).
    CAN_USE_SELF_COMPILE: "self_compile_enabled",
    CAN_VIEW_USERS_TAB: "can_view_users_tab",
    CAN_CREATE_USER: "can_create_user",
    CAN_SUSPEND_USER: "can_suspend_user",
    CAN_BAN_USER: "can_ban_user",
    CAN_APPROVE_PENDING: "can_approve_pending",
    CAN_GRANT_STONE: "can_grant_stone",
    CAN_GRANT_SELF_COMPILE: "can_grant_self_compile",
    CAN_RESTART_SERVER: "can_restart_server",
    CAN_RESET_OTHER_CREDS: "can_reset_other_creds",
}


def _has_capability_via_custom_role(user: "User", cap: str) -> bool:
    """Resolve `cap` for a user with a non-null custom_role.

    Rules:
      * Forbidden caps are always denied (server-settings, admin lifecycle).
      * Read-only "free" caps inherited from the base ceiling — view_app,
        view_profile, run_conversion — pass through if the base allows them.
      * Customizable caps require BOTH the custom role's flag to be on AND
        the base role to allow that capability (so a base=user role can't
        ever flip can_ban_user=true and gain admin powers).
      * CAN_USE_STONE and CAN_USE_SELF_COMPILE are special — they're
        grantable on top of any base, no ceiling check (matches the
        per-user grants behavior).
    """
    cr = user.custom_role
    if cap in _CUSTOM_FORBIDDEN:
        return False
    base_caps = _ROLE_CAPS.get(cr.base_role, set())

    # Free pass-through caps (no flag, ceiling-only).
    if cap in (CAN_VIEW_APP, CAN_VIEW_PROFILE, CAN_RUN_CONVERSION):
        return cap in base_caps

    flag_attr = _CUSTOM_FLAG_FOR_CAP.get(cap)
    if flag_attr is None:
        # Capability isn't customizable; fall back to whether the base
        # has it (and the per-user override layer below).
        return cap in base_caps

    flag_on = bool(getattr(cr, flag_attr, False))
    if cap == CAN_USE_STONE:
        return flag_on
    if cap == CAN_USE_SELF_COMPILE:
        # Same invariant as the per-user grant: SC implies Stone.
        return flag_on and bool(cr.stone_enabled)
    # Admin-tier caps — ceiling-checked.
    return flag_on and (cap in base_caps)


def has_capability(user: "User", cap: str) -> bool:
    if user is None or user.status != Status.active:
        return False
    if user.custom_role is not None:
        if _has_capability_via_custom_role(user, cap):
            return True
        # Per-user grants still override (e.g. an admin granted Stone
        # individually keeps it even with a custom role).
    else:
        base = _ROLE_CAPS.get(user.role, set())
        if cap in base:
            return True
    # Per-user grants override base / custom for stone / self-compile.
    if cap == CAN_USE_STONE and user.stone_enabled:
        return True
    if cap == CAN_USE_SELF_COMPILE and user.self_compile_enabled and user.stone_enabled:
        return True
    return False


def role_capabilities(role: Role) -> set[str]:
    return set(_ROLE_CAPS.get(role, set()))


def is_super_admin(user: "User") -> bool:
    return user is not None and user.role == Role.super_admin


def is_admin_or_above(user: "User") -> bool:
    return user is not None and user.role in (Role.super_admin, Role.admin)
