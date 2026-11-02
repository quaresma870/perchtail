import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, Role, User
from app.auth.providers.local import create_local_user, hash_password
from app.auth.rbac import require_global_capability, require_role_within_authority
from app.db import get_session

router = APIRouter(prefix="/users", tags=["users"])

require_manage = require_global_capability(GlobalCapability.manage_users, get_current_active_user)


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role_id: int
    active: bool
    must_change_password: bool


class UserCreate(BaseModel):
    username: str
    password: str
    role_id: int


class UserUpdate(BaseModel):
    role_id: int | None = None
    active: bool | None = None


class ResetPasswordResponse(BaseModel):
    temporary_password: str


def _get_manageable_user(session: Session, actor: User, user_id: int) -> User:
    """A delegated admin can't act on an account more privileged than
    themselves -- resetting a super-admin's password, for example, would
    otherwise hand the account (and its role) to whoever holds
    manage_users. See rbac.role_within_authority."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    require_role_within_authority(actor, target.role)
    return target


def _require_not_last_super_admin(session: Session, target: User) -> None:
    """Refuses to deactivate or demote the only remaining active
    super-admin -- the break-glass account must always exist."""
    if not target.role.is_super_admin:
        return
    others = session.exec(
        select(User)
        .join(Role)
        .where(Role.is_super_admin.is_(True), User.active.is_(True), User.id != target.id)
    ).first()
    if others is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove the last active super-admin",
        )


@router.get("", response_model=list[UserPublic])
def list_users(user: User = Depends(require_manage), session: Session = Depends(get_session)):
    return session.exec(select(User)).all()


@router.post("", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    role = session.get(Role, payload.role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    require_role_within_authority(user, role)
    if session.exec(select(User).where(User.username == payload.username)).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already in use")

    return create_local_user(
        session,
        actor_user_id=user.id,
        username=payload.username,
        password=payload.password,
        role_id=payload.role_id,
    )


@router.patch("/{user_id}", response_model=UserPublic)
def update_user(
    user_id: int,
    payload: UserUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    target = _get_manageable_user(session, user, user_id)

    if payload.role_id is not None:
        new_role = session.get(Role, payload.role_id)
        if new_role is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
        require_role_within_authority(user, new_role)
        if not new_role.is_super_admin:
            _require_not_last_super_admin(session, target)
        target.role_id = payload.role_id
    if payload.active is False:
        _require_not_last_super_admin(session, target)
    if payload.active is not None:
        target.active = payload.active

    session.add(target)
    record_audit_event(
        session,
        user_id=user.id,
        action="user.update",
        target_type="user",
        target_id=target.id,
        metadata={"role_id": target.role_id, "active": target.active},
    )
    session.commit()
    session.refresh(target)
    return target


@router.post("/{user_id}/reset-password", response_model=ResetPasswordResponse)
def reset_password(
    user_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Admin-triggered reset: issues a random temporary password (shown once,
    same as account creation) and forces a change on next login — same
    Security-notes rule as admin-created accounts."""
    target = _get_manageable_user(session, user, user_id)

    temporary_password = secrets.token_urlsafe(12)
    target.password_hash = hash_password(temporary_password)
    target.must_change_password = True
    session.add(target)
    record_audit_event(
        session,
        user_id=user.id,
        action="user.reset_password",
        target_type="user",
        target_id=target.id,
    )
    session.commit()
    return ResetPasswordResponse(temporary_password=temporary_password)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_user(
    user_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Deactivate, not hard-delete — CLAUDE.md's Security notes: soft
    deactivation is the default way to remove access, keeping audit
    history intact."""
    target = _get_manageable_user(session, user, user_id)
    _require_not_last_super_admin(session, target)

    target.active = False
    session.add(target)
    record_audit_event(
        session,
        user_id=user.id,
        action="user.deactivate",
        target_type="user",
        target_id=target.id,
    )
    session.commit()
