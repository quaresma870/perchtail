from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from sqlmodel import Session, select

from app.audit import record_audit_event
from app.auth.models import Capability, GlobalCapability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Folder, Source


def _find_grant(session: Session, *, role_id: int, scope_type: ScopeType, scope_id: int):
    return session.exec(
        select(RoleGrant).where(
            RoleGrant.role_id == role_id,
            RoleGrant.scope_type == scope_type,
            RoleGrant.scope_id == scope_id,
        )
    ).first()


def resolve_folder_or_customer_capability(
    session: Session,
    user: User,
    *,
    folder_id: int | None,
    customer_id: int | None,
    capability: Capability,
) -> bool:
    """Shared tail of the grant-resolution walk (folder chain, nearest first,
    then the customer) — used both by `resolve_capability` (for a source's
    own folder/customer) and directly by the folders API (for a folder that
    isn't attached to a source). Super-admin short-circuit and any
    source-scoped grant are the caller's concern, not this helper's."""
    role_id = user.role_id

    while folder_id is not None:
        grant = _find_grant(
            session, role_id=role_id, scope_type=ScopeType.folder, scope_id=folder_id
        )
        if grant is not None:
            return capability in grant.capabilities
        folder_id = session.get(Folder, folder_id).parent_folder_id

    if customer_id is not None:
        grant = _find_grant(
            session, role_id=role_id, scope_type=ScopeType.customer, scope_id=customer_id
        )
        if grant is not None:
            return capability in grant.capabilities

    return False


def resolve_capability(
    session: Session, user: User, source: Source, capability: Capability
) -> bool:
    """Grant-resolution logic per CLAUDE.md's "Access control (RBAC + SSO)"
    section: no grant can reach a system source short of super-admin; the
    most specific scope wins — a source-scoped grant beats a folder-scoped
    one, which beats a (possibly deeper) parent folder's, which beats the
    customer-scoped one; otherwise deny."""
    if source.is_system:
        return user.role.is_super_admin

    if user.role.is_super_admin:
        return True

    grant = _find_grant(
        session, role_id=user.role_id, scope_type=ScopeType.source, scope_id=source.id
    )
    if grant is not None:
        return capability in grant.capabilities

    return resolve_folder_or_customer_capability(
        session,
        user,
        folder_id=source.folder_id,
        customer_id=source.customer_id,
        capability=capability,
    )


def visible_source_ids(
    session: Session, user: User, capability: Capability = Capability.view
) -> set[int] | None:
    """Returns the set of non-system source ids the user holds `capability`
    on, or None to mean "all" (super-admin). Used to RBAC-filter the sources
    list endpoint without duplicating a per-source check at the call site."""
    if user.role.is_super_admin:
        return None

    ids: set[int] = set()
    for source in session.exec(select(Source).where(Source.is_system.is_(False))).all():
        if resolve_capability(session, user, source, capability):
            ids.add(source.id)
    return ids


def visible_customer_ids(session: Session, user: User) -> set[int] | None:
    """Returns the set of customer ids the user has any grant reaching
    (customer-scoped, or a folder/source grant somewhere underneath it), or
    None for "all" (super-admin). A grant here only proves the org unit is
    worth showing in a list — the actual capability is still checked at the
    source/folder endpoints themselves."""
    if user.role.is_super_admin:
        return None

    ids: set[int] = set()
    grants = session.exec(select(RoleGrant).where(RoleGrant.role_id == user.role_id)).all()
    for grant in grants:
        if grant.scope_type == ScopeType.customer:
            ids.add(grant.scope_id)
        elif grant.scope_type == ScopeType.folder:
            folder = session.get(Folder, grant.scope_id)
            if folder is not None:
                ids.add(folder.customer_id)
        elif grant.scope_type == ScopeType.source:
            source = session.get(Source, grant.scope_id)
            if source is not None and source.customer_id is not None:
                ids.add(source.customer_id)
    return ids


def require_capability(capability: Capability, get_current_user: Callable[..., User]):
    """FastAPI dependency factory wrapping grant resolution. `get_current_user`
    is injected by the caller rather than hardcoded here, since session/token
    issuance (api/auth.py) isn't built yet — this lets routes wire in
    whichever current-user dependency exists once it is."""

    def dependency(
        source_id: int,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> Source:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
        if not resolve_capability(session, user, source, capability):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
        return source

    return dependency


def require_global_capability(capability: GlobalCapability, get_current_user: Callable[..., User]):
    """FastAPI dependency factory for endpoints gated by a global capability
    (manage_users, manage_roles, manage_sso, create_source) rather than a
    per-source/folder/customer grant — e.g. the users/roles/customers admin
    surfaces. Super-admins always pass, same as everywhere else."""

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.is_super_admin:
            return user
        if capability in user.role.global_capabilities:
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")

    return dependency


def create_role(
    session: Session,
    *,
    actor_user_id: int | None,
    name: str,
    is_builtin: bool = False,
    is_super_admin: bool = False,
    global_capabilities: list[GlobalCapability] | None = None,
) -> Role:
    role = Role(
        name=name,
        is_builtin=is_builtin,
        is_super_admin=is_super_admin,
        global_capabilities=global_capabilities or [],
    )
    session.add(role)
    session.flush()

    record_audit_event(
        session,
        user_id=actor_user_id,
        action="role.create",
        target_type="role",
        target_id=role.id,
        metadata={"name": name},
    )
    session.commit()
    session.refresh(role)
    return role


def create_role_grant(
    session: Session,
    *,
    actor_user_id: int | None,
    role_id: int,
    scope_type: ScopeType,
    scope_id: int,
    capabilities: list[Capability],
) -> RoleGrant:
    grant = RoleGrant(
        role_id=role_id,
        scope_type=scope_type,
        scope_id=scope_id,
        capabilities=capabilities,
    )
    session.add(grant)
    session.flush()

    record_audit_event(
        session,
        user_id=actor_user_id,
        action="role_grant.create",
        target_type="role_grant",
        target_id=grant.id,
        metadata={"role_id": role_id, "scope_type": scope_type, "scope_id": scope_id},
    )
    session.commit()
    session.refresh(grant)
    return grant
