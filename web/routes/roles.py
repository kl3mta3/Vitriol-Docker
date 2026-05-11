"""Custom-role CRUD — super admin only.

Custom roles let the operator name a permission profile ("an admin with
modifications, or less") and assign it to users instead of relying on the
five built-in roles. The base_role chosen at creation time sets the
ceiling; flag fields let the super admin dial individual capabilities up
or down within that ceiling. Server-settings access and admin-lifecycle
powers are super-admin-only invariants — the API rejects any attempt to
flip those on.
"""
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..deps import get_db, require_super_admin
from ..models import CustomRole, Role, ServerSettings, User
from ..schemas import (
    CustomRoleCreateRequest, CustomRoleOut, CustomRoleUpdateRequest, MessageResponse,
)
from ..services import audit

router = APIRouter(prefix="/roles", tags=["roles"])


# Base roles a custom role is allowed to inherit from. Super admin can
# never be a base — that role is reserved.
_VALID_BASES = {"admin", "user", "viewer", "pending"}


def _to_out(cr: CustomRole, user_count: int) -> CustomRoleOut:
    return CustomRoleOut(
        id=cr.id,
        name=cr.name,
        description=cr.description,
        base_role=cr.base_role.value if hasattr(cr.base_role, "value") else str(cr.base_role),
        stone_enabled=cr.stone_enabled,
        self_compile_enabled=cr.self_compile_enabled,
        daily_conversion_limit=cr.daily_conversion_limit,
        rate_limit_per_minute=cr.rate_limit_per_minute,
        can_create_user=cr.can_create_user,
        can_suspend_user=cr.can_suspend_user,
        can_ban_user=cr.can_ban_user,
        can_approve_pending=cr.can_approve_pending,
        can_grant_stone=cr.can_grant_stone,
        can_grant_self_compile=cr.can_grant_self_compile,
        can_restart_server=cr.can_restart_server,
        can_view_users_tab=cr.can_view_users_tab,
        can_reset_other_creds=cr.can_reset_other_creds,
        can_view_own_files=cr.can_view_own_files,
        can_view_others_files=cr.can_view_others_files,
        can_download_others_files=cr.can_download_others_files,
        can_delete_others_files=cr.can_delete_others_files,
        can_set_user_file_size_cap=cr.can_set_user_file_size_cap,
        max_file_size_bytes=cr.max_file_size_bytes,
        created_at=cr.created_at,
        user_count=user_count,
    )


def _validate_base(base: str) -> Role:
    if base not in _VALID_BASES:
        raise HTTPException(
            status_code=400,
            detail=f"base_role must be one of {sorted(_VALID_BASES)}; got {base!r}",
        )
    return Role(base)


def _enforce_admin_only_flags(payload, base: Role) -> None:
    """If the base role can't reach admin tier, reject any admin-tier flag.

    Defense-in-depth on top of the permission resolver — keeps the DB
    state honest so casual eyeballing of the row doesn't suggest powers
    that aren't actually grantable.
    """
    if base == Role.admin:
        return
    admin_only = (
        "can_create_user", "can_suspend_user", "can_ban_user",
        "can_approve_pending", "can_grant_stone", "can_grant_self_compile",
        "can_restart_server", "can_view_users_tab", "can_reset_other_creds",
        # The *_others files caps are admin-tier — viewing/touching
        # someone else's outputs requires the base to permit it.
        "can_view_others_files", "can_download_others_files",
        "can_delete_others_files",
        # Editing another user's max_file_size_bytes is admin-tier too;
        # a user-base role can never grant cross-user write access.
        "can_set_user_file_size_cap",
    )
    on = [f for f in admin_only if getattr(payload, f, False)]
    if on:
        raise HTTPException(
            status_code=400,
            detail=(f"Flags {on} require base_role='admin'. Either change the base "
                    f"or turn those flags off."),
        )


@router.get("", response_model=List[CustomRoleOut])
def list_roles(actor: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(CustomRole, func.count(User.id).label("uc"))
        .outerjoin(User, User.custom_role_id == CustomRole.id)
        .group_by(CustomRole.id)
        .order_by(CustomRole.name)
        .all()
    )
    return [_to_out(r, int(uc or 0)) for (r, uc) in rows]


@router.post("", response_model=CustomRoleOut)
def create_role(
    req: CustomRoleCreateRequest,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    base = _validate_base(req.base_role)
    _enforce_admin_only_flags(req, base)
    if db.query(CustomRole).filter(CustomRole.name == req.name).count():
        raise HTTPException(status_code=409, detail="A role with that name already exists.")
    cr = CustomRole(
        name=req.name,
        description=req.description,
        base_role=base,
        stone_enabled=req.stone_enabled,
        self_compile_enabled=req.self_compile_enabled,
        daily_conversion_limit=req.daily_conversion_limit,
        rate_limit_per_minute=req.rate_limit_per_minute,
        max_file_size_bytes=req.max_file_size_bytes if req.max_file_size_bytes and req.max_file_size_bytes > 0 else None,
        can_create_user=req.can_create_user,
        can_suspend_user=req.can_suspend_user,
        can_ban_user=req.can_ban_user,
        can_approve_pending=req.can_approve_pending,
        can_grant_stone=req.can_grant_stone,
        can_grant_self_compile=req.can_grant_self_compile,
        can_restart_server=req.can_restart_server,
        can_view_users_tab=req.can_view_users_tab,
        can_reset_other_creds=req.can_reset_other_creds,
        can_view_own_files=req.can_view_own_files,
        can_view_others_files=req.can_view_others_files,
        can_download_others_files=req.can_download_others_files,
        can_delete_others_files=req.can_delete_others_files,
        can_set_user_file_size_cap=req.can_set_user_file_size_cap,
        created_by_user_id=actor.id,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)
    audit.log(db, actor.id, "custom_role_create", metadata={"role_id": cr.id, "name": cr.name})
    return _to_out(cr, 0)


@router.patch("/{role_id}", response_model=CustomRoleOut)
def update_role(
    role_id: int,
    req: CustomRoleUpdateRequest,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    cr: Optional[CustomRole] = db.query(CustomRole).get(role_id)
    if cr is None:
        raise HTTPException(status_code=404, detail="Role not found")

    if req.name is not None and req.name != cr.name:
        if db.query(CustomRole).filter(CustomRole.name == req.name, CustomRole.id != role_id).count():
            raise HTTPException(status_code=409, detail="A role with that name already exists.")
        cr.name = req.name
    if req.description is not None:
        cr.description = req.description
    if req.base_role is not None:
        cr.base_role = _validate_base(req.base_role)

    bool_fields = (
        "stone_enabled", "self_compile_enabled",
        "can_create_user", "can_suspend_user", "can_ban_user",
        "can_approve_pending", "can_grant_stone", "can_grant_self_compile",
        "can_restart_server", "can_view_users_tab", "can_reset_other_creds",
        "can_view_own_files", "can_view_others_files",
        "can_download_others_files", "can_delete_others_files",
        "can_set_user_file_size_cap",
    )
    for f in bool_fields:
        v = getattr(req, f)
        if v is not None:
            setattr(cr, f, v)
    if req.daily_conversion_limit is not None:
        cr.daily_conversion_limit = req.daily_conversion_limit
    if req.rate_limit_per_minute is not None:
        cr.rate_limit_per_minute = req.rate_limit_per_minute
    if req.max_file_size_bytes is not None:
        # 0 clears the role-level override; positive sets it.
        cr.max_file_size_bytes = req.max_file_size_bytes if req.max_file_size_bytes > 0 else None

    # Re-validate after applying changes.
    _enforce_admin_only_flags(cr, cr.base_role)

    db.commit()
    db.refresh(cr)
    uc = db.query(func.count(User.id)).filter(User.custom_role_id == cr.id).scalar() or 0
    audit.log(db, actor.id, "custom_role_update", metadata={"role_id": cr.id})
    return _to_out(cr, int(uc))


@router.delete("/{role_id}", response_model=MessageResponse)
def delete_role(
    role_id: int,
    actor: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    cr: Optional[CustomRole] = db.query(CustomRole).get(role_id)
    if cr is None:
        raise HTTPException(status_code=404, detail="Role not found")
    # If this role is the signup default, clear that pointer first so
    # the FK doesn't dangle.
    s = db.query(ServerSettings).get(1)
    if s is not None and s.signup_default_custom_role_id == role_id:
        s.signup_default_custom_role_id = None
    # Users with this role get unset to None (their ceiling stays in
    # `users.role`, so they fall back to the bare built-in).
    db.query(User).filter(User.custom_role_id == role_id).update(
        {User.custom_role_id: None}, synchronize_session=False
    )
    db.delete(cr)
    db.commit()
    audit.log(db, actor.id, "custom_role_delete", metadata={"role_id": role_id, "name": cr.name})
    return MessageResponse(message=f"Role {cr.name!r} deleted.")
