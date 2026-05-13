"""Admin / Super-admin user management."""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.password import hash_password
from ..auth.permissions import (
    has_capability, is_super_admin, is_sudo_admin, is_super_or_sudo, is_admin_or_above,
    CAN_CREATE_ADMIN, CAN_DELETE_ADMIN, CAN_GRANT_STONE, CAN_GRANT_SELF_COMPILE,
    CAN_RESET_OTHER_CREDS, CAN_SET_USER_FILE_SIZE_CAP, CAN_SET_USER_RETENTION,
)
from ..deps import get_current_user, get_db, require_admin, require_super_admin
from ..models import ApprovalRequest, ApprovalStatus, Role, Status, User
from ..schemas import (
    CredentialResetRequest, MessageResponse, SuspendRequest, UserCreateRequest,
    UserOut, UserUpdateRequest,
)
from ..services import audit
from ..services.suspensions import expiry_for

router = APIRouter(prefix="/users", tags=["users"])


def _ensure_can_modify(actor: User, target: User, allow_admin_modify_admin: bool = False) -> None:
    """Common rule set for actions targeting another user.

    Super admins can modify anyone.
    Sudo admins can modify other admins but never the super admin.
    Regular admins can modify non-admins (and, with allow_admin_modify_admin,
    perform the narrow suspend/unsuspend actions on fellow admins).
    """
    if target.role == Role.super_admin and not is_super_admin(actor):
        raise HTTPException(status_code=403, detail="Only the super admin can modify the super admin.")
    if target.role == Role.admin and not is_super_or_sudo(actor) and not allow_admin_modify_admin:
        if actor.id != target.id:
            raise HTTPException(status_code=403, detail="Admins cannot modify other admins.")


@router.get("", response_model=List[UserOut])
def list_users(
    q: Optional[str] = None,
    include_unverified: bool = False,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List users.

    ``q`` — case-insensitive substring match across username, email,
    first_name, last_name. Drives the search box in the admin Users
    tab. Empty / missing means "no filter".
    """
    query = db.query(User)
    # Regular admins must not see (or be able to act on) the super admin.
    if not is_super_admin(actor):
        query = query.filter(User.role != Role.super_admin)
    # Unverified rows (signup awaiting email verification) are hidden by
    # default — they're a transient state that auto-purges after 24h, and
    # admins don't want them cluttering the user table or inflating the
    # user count. Pass ?include_unverified=1 to see them.
    if not include_unverified:
        query = query.filter(User.status != Status.unverified)
    if q:
        from sqlalchemy import or_, func as _sa_func
        needle = f"%{q.strip().lower()}%"
        query = query.filter(
            or_(
                _sa_func.lower(User.username).like(needle),
                _sa_func.lower(User.email).like(needle),
                _sa_func.lower(User.first_name).like(needle),
                _sa_func.lower(User.last_name).like(needle),
            )
        )
    return query.order_by(User.id).all()


@router.post("", response_model=UserOut)
def create_user(req: UserCreateRequest, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target_role = Role(req.role) if req.role else Role.user
    if target_role == Role.super_admin:
        raise HTTPException(status_code=400, detail="Cannot create another super admin.")
    if target_role == Role.admin and not has_capability(actor, CAN_CREATE_ADMIN):
        raise HTTPException(status_code=403, detail="Only the super admin can create admins.")
    if db.query(User).filter(User.username == req.username).count():
        raise HTTPException(status_code=409, detail="Username taken")
    if req.email and db.query(User).filter(User.email == req.email).count():
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        username=req.username,
        first_name=(req.first_name or None) and req.first_name.strip() or None,
        last_name=(req.last_name or None) and req.last_name.strip() or None,
        email=req.email,
        password_hash=hash_password(req.password) if req.password else None,
        role=target_role,
        status=Status.active,
        created_by_user_id=actor.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit.log(db, actor.id, "user_create", target_user_id=user.id, metadata={"role": user.role.value})
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Don't leak super admin existence to regular admins. 404, not 403,
    # so they can't probe to discover the super admin's id.
    if target.role == Role.super_admin and not is_super_admin(actor):
        raise HTTPException(status_code=404, detail="User not found")
    return target


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, req: UserUpdateRequest, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    _ensure_can_modify(actor, target)

    if req.role is not None:
        new_role = Role(req.role)
        if new_role == Role.super_admin:
            raise HTTPException(status_code=400, detail="Cannot promote to super admin.")
        if target.role == Role.super_admin:
            raise HTTPException(status_code=400, detail="Cannot demote the super admin.")
        if (new_role == Role.admin or target.role == Role.admin) and not has_capability(actor, CAN_CREATE_ADMIN):
            raise HTTPException(status_code=403, detail="Only a super admin or sudo admin can change admin roles.")
        target.role = new_role
        # Clear sudo flag when demoting away from admin — the flag has no
        # effect on non-admins but keeping it set would be confusing if the
        # user is later re-promoted.
        if new_role != Role.admin:
            target.is_sudo_admin = False
    if req.custom_role_id is not None:
        # 0 (or any falsy) clears the assignment; positive ints look up
        # the role and assign it. Super admins and sudo admins can assign
        # any custom role; regular admins cannot.
        if not is_super_or_sudo(actor):
            raise HTTPException(status_code=403, detail="Only a super admin or sudo admin can assign custom roles.")
        from ..models import CustomRole as _CR
        if req.custom_role_id == 0:
            target.custom_role_id = None
        else:
            cr = db.query(_CR).get(req.custom_role_id)
            if cr is None:
                raise HTTPException(status_code=400, detail="custom_role_id does not exist.")
            target.custom_role_id = cr.id
            # Keep target.role aligned with the custom role's ceiling so
            # role-based queries stay accurate.
            target.role = cr.base_role
    if req.email is not None:
        # Collision check — refuse if another row already owns this
        # email. Empty-string `req.email` clears the column and is
        # never a collision. The .id != target.id guard lets an admin
        # "Save" a user without ever changing the email — the row's
        # own existing email obviously matches itself but isn't a clash.
        new_email = req.email or None
        if new_email and db.query(User).filter(
            User.email == new_email, User.id != target.id
        ).count():
            raise HTTPException(status_code=409, detail="Email already registered to another user.")
        target.email = new_email
    if req.first_name is not None:
        # Empty string explicitly clears the saved name; non-empty
        # sets it. Same semantics as the email field above.
        target.first_name = req.first_name.strip() or None
    if req.last_name is not None:
        target.last_name = req.last_name.strip() or None
    if req.stone_enabled is not None:
        if not has_capability(actor, CAN_GRANT_STONE):
            raise HTTPException(status_code=403, detail="Cannot grant stone")
        target.stone_enabled = req.stone_enabled
    if req.self_compile_enabled is not None:
        if not has_capability(actor, CAN_GRANT_SELF_COMPILE):
            raise HTTPException(status_code=403, detail="Cannot grant self-compile")
        target.self_compile_enabled = req.self_compile_enabled
    if req.daily_conversion_limit is not None:
        target.daily_conversion_limit = req.daily_conversion_limit
    if req.rate_limit_per_minute is not None:
        target.rate_limit_per_minute = req.rate_limit_per_minute
    if req.max_file_size_bytes is not None:
        # Capability-gated: must be admin / super_admin or hold the
        # explicit `set_user_file_size_cap` flag via a custom role.
        # 403 instead of silently ignoring so the operator knows the
        # field didn't apply.
        if not has_capability(actor, CAN_SET_USER_FILE_SIZE_CAP):
            raise HTTPException(
                status_code=403,
                detail="Cannot change max_file_size_bytes — missing set_user_file_size_cap capability.",
            )
        # 0 explicitly clears the override (back to role/server default).
        target.max_file_size_bytes = req.max_file_size_bytes if req.max_file_size_bytes > 0 else None
    if req.max_output_size_bytes is not None:
        # Same capability gate as max_file_size_bytes — both knobs are
        # the "raise the per-user limit" pattern and any admin trusted
        # with one is trusted with the others.
        if not has_capability(actor, CAN_SET_USER_FILE_SIZE_CAP):
            raise HTTPException(
                status_code=403,
                detail="Cannot change max_output_size_bytes — missing set_user_file_size_cap capability.",
            )
        target.max_output_size_bytes = req.max_output_size_bytes if req.max_output_size_bytes > 0 else None
    if req.max_storage_bytes is not None:
        if not has_capability(actor, CAN_SET_USER_FILE_SIZE_CAP):
            raise HTTPException(
                status_code=403,
                detail="Cannot change max_storage_bytes — missing set_user_file_size_cap capability.",
            )
        target.max_storage_bytes = req.max_storage_bytes if req.max_storage_bytes > 0 else None
    if req.output_retention is not None:
        # Capability-gated mirror of max_file_size_bytes above. Empty dict
        # explicitly clears the per-user override (falls back to custom
        # role → server default for the user's role).
        if not has_capability(actor, CAN_SET_USER_RETENTION):
            raise HTTPException(
                status_code=403,
                detail="Cannot change output_retention — missing set_user_retention capability.",
            )
        if req.output_retention:
            import json as _json
            # Only persist the canonical keys so callers can't smuggle
            # extra junk into the blob.
            cleaned = {
                k: v for k, v in req.output_retention.items()
                if k in ("max_files", "max_age", "age_unit", "delete_on_download")
            }
            target.output_retention_json = _json.dumps(cleaned) if cleaned else None
        else:
            target.output_retention_json = None

    if req.is_sudo_admin is not None:
        # Only the super admin can grant or revoke sudo status.
        # Sudo flag only makes sense on admin-role users — silently ignore
        # the request for other roles to avoid confusing state.
        if not is_super_admin(actor):
            raise HTTPException(status_code=403, detail="Only the super admin can set sudo admin status.")
        if target.role == Role.admin:
            target.is_sudo_admin = req.is_sudo_admin
        # Non-admin targets: ignore silently (the flag has no effect there
        # anyway, but don't error — the caller may be changing a role and
        # setting sudo in the same PATCH).

    db.commit()
    db.refresh(target)
    audit.log(db, actor.id, "user_update", target_user_id=target.id)
    return target


@router.delete("/{user_id}", response_model=MessageResponse)
def delete_user(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == Role.super_admin:
        raise HTTPException(status_code=400, detail="The super admin cannot be deleted.")
    if target.role == Role.admin and not has_capability(actor, CAN_DELETE_ADMIN):
        raise HTTPException(status_code=403, detail="Only the super admin can delete admins.")
    if target.id == actor.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself.")
    db.delete(target)
    db.commit()
    audit.log(db, actor.id, "user_delete", target_user_id=user_id)
    return MessageResponse(message="User deleted.")


# ----------------------------------------------------- suspend / ban

@router.post("/{user_id}/suspend", response_model=UserOut)
def suspend(user_id: int, req: SuspendRequest, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == Role.super_admin:
        raise HTTPException(status_code=400, detail="The super admin cannot be suspended.")
    _ensure_can_modify(actor, target, allow_admin_modify_admin=(target.role == Role.admin))
    try:
        until = expiry_for(req.duration)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    target.status = Status.suspended
    target.suspended_until = until
    target.suspension_reason = req.reason
    db.commit()
    db.refresh(target)
    audit.log(db, actor.id, "user_suspend", target_user_id=target.id, metadata={"duration": req.duration})
    return target


@router.post("/{user_id}/unsuspend", response_model=UserOut)
def unsuspend(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    _ensure_can_modify(actor, target, allow_admin_modify_admin=(target.role == Role.admin))
    if target.status != Status.suspended:
        raise HTTPException(status_code=400, detail="User is not suspended.")
    target.status = Status.active
    target.suspended_until = None
    target.suspension_reason = None
    db.commit()
    db.refresh(target)
    audit.log(db, actor.id, "user_unsuspend", target_user_id=target.id)
    return target


@router.post("/{user_id}/ban", response_model=UserOut)
def ban(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == Role.super_admin:
        raise HTTPException(status_code=400, detail="The super admin cannot be banned.")
    if target.role == Role.admin and not is_super_or_sudo(actor):
        raise HTTPException(status_code=403, detail="Only a super admin or sudo admin can ban an admin.")
    target.status = Status.banned
    db.commit()
    db.refresh(target)
    audit.log(db, actor.id, "user_ban", target_user_id=target.id)
    return target


@router.post("/{user_id}/unban", response_model=UserOut)
def unban(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == Role.admin and not is_super_or_sudo(actor):
        raise HTTPException(status_code=403, detail="Only a super admin or sudo admin can unban an admin.")
    if target.status != Status.banned:
        raise HTTPException(status_code=400, detail="User is not banned.")
    target.status = Status.active
    db.commit()
    db.refresh(target)
    audit.log(db, actor.id, "user_unban", target_user_id=target.id)
    return target


# ---------------------------------------------------------- approvals

class _ApproveBody(BaseModel):
    role: str = "user"


@router.post("/{user_id}/approve", response_model=UserOut)
async def approve_pending(
    user_id: int,
    body: Optional[_ApproveBody] = None,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role != Role.pending:
        raise HTTPException(status_code=400, detail="User is not pending.")
    requested = (body.role if body else "user").lower()
    try:
        new_role = Role(requested)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown role: {requested!r}")
    if new_role not in (Role.viewer, Role.user, Role.admin):
        raise HTTPException(status_code=400, detail=f"Cannot approve into {new_role.value!r}.")
    if new_role == Role.admin and not is_super_or_sudo(actor):
        raise HTTPException(status_code=403, detail="Only a super admin or sudo admin can approve into admin.")
    target.role = new_role
    ar = (
        db.query(ApprovalRequest)
        .filter(ApprovalRequest.user_id == user_id, ApprovalRequest.status == ApprovalStatus.pending)
        .one_or_none()
    )
    if ar:
        ar.status = ApprovalStatus.approved
        ar.decided_by_user_id = actor.id
        ar.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(target)
    audit.log(db, actor.id, "user_approve", target_user_id=target.id, metadata={"role": new_role.value})

    # Auto-provision into every OIDC provider that's opted in. Per-row
    # failures don't roll back the approval — admin gets the failure
    # detail in the audit log so they can manually create the missing
    # IdP account if needed. (Blocking the approval on an IdP-side
    # network blip would be worse UX than completing the approval and
    # flagging the provisioning failure.)
    from ..services.provisioning import provision_user_to_all_enabled
    try:
        results = await provision_user_to_all_enabled(db, target)
        for slug, ok, err in results:
            audit.log(
                db, actor.id,
                "provision_ok" if ok else "provision_failed",
                target_user_id=target.id,
                metadata={"provider": slug, "error": err} if err else {"provider": slug},
            )
    except Exception:
        # provision_user_to_all_enabled already swallows per-row
        # exceptions; this catch only fires on a catastrophic failure
        # (DB session gone, etc.) which we just log and move on.
        import logging
        logging.getLogger("vitriol.provisioning").exception(
            "Provisioning fan-out crashed for user %s after approval", target.id,
        )

    return target


@router.post("/{user_id}/deny", response_model=MessageResponse)
def deny_pending(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role != Role.pending:
        raise HTTPException(status_code=400, detail="User is not pending.")
    ar = (
        db.query(ApprovalRequest)
        .filter(ApprovalRequest.user_id == user_id, ApprovalRequest.status == ApprovalStatus.pending)
        .one_or_none()
    )
    if ar:
        ar.status = ApprovalStatus.denied
        ar.decided_by_user_id = actor.id
        ar.decided_at = datetime.utcnow()
    db.delete(target)
    db.commit()
    audit.log(db, actor.id, "user_deny", target_user_id=user_id)
    return MessageResponse(message="User denied and removed.")


# ----------------------------------------- credential reset (by admin)

@router.post("/{user_id}/reset-credentials", response_model=UserOut)
def reset_credentials(
    user_id: int, req: CredentialResetRequest,
    actor: User = Depends(require_admin), db: Session = Depends(get_db),
):
    target: Optional[User] = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == Role.super_admin and not is_super_admin(actor):
        raise HTTPException(status_code=403, detail="Only the super admin can reset the super admin's creds.")
    if target.role == Role.admin and not is_super_or_sudo(actor):
        raise HTTPException(status_code=403, detail="Only a super admin or sudo admin can reset another admin's credentials.")
    if target.role in (Role.user, Role.pending, Role.viewer) and not has_capability(actor, CAN_RESET_OTHER_CREDS):
        raise HTTPException(status_code=403, detail="Cannot reset other users' credentials.")
    if req.new_username:
        if db.query(User).filter(User.username == req.new_username, User.id != target.id).count():
            raise HTTPException(status_code=409, detail="Username taken")
        target.username = req.new_username
    if req.new_email is not None:
        # Same collision check as the PATCH route. Empty/null clears
        # the column and is never a clash.
        new_email = req.new_email or None
        if new_email and db.query(User).filter(
            User.email == new_email, User.id != target.id
        ).count():
            raise HTTPException(status_code=409, detail="Email already registered to another user.")
        target.email = new_email
    if req.new_password:
        target.password_hash = hash_password(req.new_password)
    db.commit()
    db.refresh(target)
    audit.log(db, actor.id, "credentials_reset", target_user_id=target.id)
    return target


# ----------------------------------------- per-user grant shortcuts

@router.post("/{user_id}/grant-stone", response_model=UserOut)
def grant_stone(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    if not has_capability(actor, CAN_GRANT_STONE):
        raise HTTPException(status_code=403, detail="Cannot grant stone")
    target = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    target.stone_enabled = True
    db.commit(); db.refresh(target)
    audit.log(db, actor.id, "grant_stone", target_user_id=target.id)
    return target


@router.post("/{user_id}/revoke-stone", response_model=UserOut)
def revoke_stone(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    if not has_capability(actor, CAN_GRANT_STONE):
        raise HTTPException(status_code=403, detail="Cannot revoke stone")
    target = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    target.stone_enabled = False
    target.self_compile_enabled = False  # implies revoke
    db.commit(); db.refresh(target)
    audit.log(db, actor.id, "revoke_stone", target_user_id=target.id)
    return target


@router.post("/{user_id}/grant-self-compile", response_model=UserOut)
def grant_self_compile(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    if not has_capability(actor, CAN_GRANT_SELF_COMPILE):
        raise HTTPException(status_code=403, detail="Cannot grant self-compile")
    target = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    target.stone_enabled = True
    target.self_compile_enabled = True
    db.commit(); db.refresh(target)
    audit.log(db, actor.id, "grant_self_compile", target_user_id=target.id)
    return target


@router.post("/{user_id}/revoke-self-compile", response_model=UserOut)
def revoke_self_compile(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    if not has_capability(actor, CAN_GRANT_SELF_COMPILE):
        raise HTTPException(status_code=403, detail="Cannot revoke self-compile")
    target = db.query(User).get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    target.self_compile_enabled = False
    db.commit(); db.refresh(target)
    audit.log(db, actor.id, "revoke_self_compile", target_user_id=target.id)
    return target
