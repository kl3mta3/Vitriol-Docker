"""Admin / Super-admin user management."""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.password import hash_password
from ..auth.permissions import (
    has_capability, is_super_admin, is_admin_or_above,
    CAN_CREATE_ADMIN, CAN_DELETE_ADMIN, CAN_GRANT_STONE, CAN_GRANT_SELF_COMPILE,
    CAN_RESET_OTHER_CREDS,
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
    """Common rule set for actions targeting another user."""
    if target.role == Role.super_admin and not is_super_admin(actor):
        raise HTTPException(status_code=403, detail="Only the super admin can modify the super admin.")
    if target.role == Role.admin and not is_super_admin(actor) and not allow_admin_modify_admin:
        if actor.id != target.id:
            raise HTTPException(status_code=403, detail="Admins cannot modify other admins.")


@router.get("", response_model=List[UserOut])
def list_users(actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    q = db.query(User)
    # Regular admins must not see (or be able to act on) the super admin.
    if not is_super_admin(actor):
        q = q.filter(User.role != Role.super_admin)
    return q.order_by(User.id).all()


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
            raise HTTPException(status_code=403, detail="Only the super admin can change admin roles.")
        target.role = new_role
    if req.custom_role_id is not None:
        # 0 (or any falsy) clears the assignment; positive ints look up
        # the role and assign it (only super admins can manage custom
        # roles on users).
        if not is_super_admin(actor):
            raise HTTPException(status_code=403, detail="Only the super admin can assign custom roles.")
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
        target.email = req.email or None
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
    if target.role == Role.admin and not is_super_admin(actor):
        raise HTTPException(status_code=403, detail="Only the super admin can ban an admin.")
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
    if target.role == Role.admin and not is_super_admin(actor):
        raise HTTPException(status_code=403, detail="Only the super admin can unban an admin.")
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
def approve_pending(
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
    if new_role == Role.admin and not is_super_admin(actor):
        raise HTTPException(status_code=403, detail="Only the super admin can approve into admin.")
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
        raise HTTPException(status_code=403, detail="Only the super admin can reset their own creds.")
    if target.role == Role.admin and not is_super_admin(actor):
        raise HTTPException(status_code=403, detail="Only the super admin can reset another admin.")
    if target.role in (Role.user, Role.pending, Role.viewer) and not has_capability(actor, CAN_RESET_OTHER_CREDS):
        raise HTTPException(status_code=403, detail="Cannot reset other users' credentials.")
    if req.new_username:
        if db.query(User).filter(User.username == req.new_username, User.id != target.id).count():
            raise HTTPException(status_code=409, detail="Username taken")
        target.username = req.new_username
    if req.new_email is not None:
        target.email = req.new_email or None
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
