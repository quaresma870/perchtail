from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.auth.models import Capability, GlobalCapability, Role, RoleGrant, ScopeType, User
from app.auth.rbac import create_role, create_role_grant, require_global_capability
from app.db import get_session
from app.models import Customer, Folder, Source

router = APIRouter(prefix="/roles", tags=["roles"])

require_manage = require_global_capability(GlobalCapability.manage_roles, get_current_active_user)


class RolePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_builtin: bool
    is_super_admin: bool
    global_capabilities: list[GlobalCapability]


class RoleCreate(BaseModel):
    name: str
    is_super_admin: bool = False
    global_capabilities: list[GlobalCapability] = []


class RoleUpdate(BaseModel):
    name: str | None = None
    is_super_admin: bool | None = None
    global_capabilities: list[GlobalCapability] | None = None


class DuplicateRoleRequest(BaseModel):
    name: str | None = None


class RoleGrantPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role_id: int
    scope_type: ScopeType
    scope_id: int
    capabilities: list[Capability]


class RoleGrantCreate(BaseModel):
    scope_type: ScopeType
    scope_id: int
    capabilities: list[Capability]


class RoleGrantUpdate(BaseModel):
    capabilities: list[Capability]


def _require_super_admin_for_super_admin_flag(user: User, is_super_admin: bool) -> None:
    # Only a super-admin can mint or edit another super-admin role — a
    # manage_roles-only admin escalating a role to is_super_admin=True would
    # otherwise be a privilege-escalation path (they could later assign that
    # role to any user, including themselves, via the users API).
    if is_super_admin and not user.role.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super-admin can create or edit a super-admin role",
        )


def _validate_scope_exists(session: Session, *, scope_type: ScopeType, scope_id: int) -> None:
    model = {
        ScopeType.customer: Customer,
        ScopeType.folder: Folder,
        ScopeType.source: Source,
    }[scope_type]
    if session.get(model, scope_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{scope_type.value} not found"
        )


@router.get("", response_model=list[RolePublic])
def list_roles(user: User = Depends(require_manage), session: Session = Depends(get_session)):
    return session.exec(select(Role)).all()


@router.post("", response_model=RolePublic, status_code=status.HTTP_201_CREATED)
def create_role_endpoint(
    payload: RoleCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    _require_super_admin_for_super_admin_flag(user, payload.is_super_admin)
    if session.exec(select(Role).where(Role.name == payload.name)).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already in use")

    return create_role(
        session,
        actor_user_id=user.id,
        name=payload.name,
        is_super_admin=payload.is_super_admin,
        global_capabilities=payload.global_capabilities,
    )


@router.patch("/{role_id}", response_model=RolePublic)
def update_role(
    role_id: int,
    payload: RoleUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    role = session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    target_super_admin = (
        payload.is_super_admin if payload.is_super_admin is not None else role.is_super_admin
    )
    _require_super_admin_for_super_admin_flag(user, target_super_admin)

    if payload.name is not None:
        role.name = payload.name
    if payload.is_super_admin is not None:
        role.is_super_admin = payload.is_super_admin
    if payload.global_capabilities is not None:
        role.global_capabilities = payload.global_capabilities

    session.add(role)
    session.commit()
    session.refresh(role)
    return role


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    role = session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete a built-in role"
        )

    if session.exec(select(User).where(User.role_id == role_id)).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role still has users assigned; reassign them first",
        )

    session.delete(role)
    session.commit()


@router.post("/{role_id}/duplicate", response_model=RolePublic, status_code=status.HTTP_201_CREATED)
def duplicate_role(
    role_id: int,
    payload: DuplicateRoleRequest,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    source_role = session.get(Role, role_id)
    if source_role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    _require_super_admin_for_super_admin_flag(user, source_role.is_super_admin)

    new_name = payload.name or f"{source_role.name} (copy)"
    if session.exec(select(Role).where(Role.name == new_name)).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Name already in use; pass an explicit name",
        )

    new_role = create_role(
        session,
        actor_user_id=user.id,
        name=new_name,
        is_super_admin=source_role.is_super_admin,
        global_capabilities=list(source_role.global_capabilities),
    )
    for grant in session.exec(select(RoleGrant).where(RoleGrant.role_id == role_id)).all():
        create_role_grant(
            session,
            actor_user_id=user.id,
            role_id=new_role.id,
            scope_type=grant.scope_type,
            scope_id=grant.scope_id,
            capabilities=list(grant.capabilities),
        )
    session.refresh(new_role)
    return new_role


@router.get("/{role_id}/grants", response_model=list[RoleGrantPublic])
def list_role_grants(
    role_id: int, user: User = Depends(require_manage), session: Session = Depends(get_session)
):
    if session.get(Role, role_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return session.exec(select(RoleGrant).where(RoleGrant.role_id == role_id)).all()


@router.post(
    "/{role_id}/grants", response_model=RoleGrantPublic, status_code=status.HTTP_201_CREATED
)
def create_grant(
    role_id: int,
    payload: RoleGrantCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    if session.get(Role, role_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    _validate_scope_exists(session, scope_type=payload.scope_type, scope_id=payload.scope_id)

    return create_role_grant(
        session,
        actor_user_id=user.id,
        role_id=role_id,
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        capabilities=payload.capabilities,
    )


@router.patch("/{role_id}/grants/{grant_id}", response_model=RoleGrantPublic)
def update_grant(
    role_id: int,
    grant_id: int,
    payload: RoleGrantUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    grant = session.get(RoleGrant, grant_id)
    if grant is None or grant.role_id != role_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")

    grant.capabilities = payload.capabilities
    session.add(grant)
    session.commit()
    session.refresh(grant)
    return grant


@router.delete("/{role_id}/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_grant(
    role_id: int,
    grant_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    grant = session.get(RoleGrant, grant_id)
    if grant is None or grant.role_id != role_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")

    session.delete(grant)
    session.commit()
