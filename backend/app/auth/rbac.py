from collections.abc import Callable
from dataclasses import dataclass, field

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


def holds_capability_at(
    session: Session,
    user: User,
    *,
    folder_id: int | None,
    customer_id: int | None,
    capability: Capability,
) -> bool:
    """Whether `user` holds `capability` at a folder (or, with no folder, at
    the customer's root), by the grant resolution a source placed there
    would get: the folder, its ancestors, then the customer. Super admins
    hold everything. Used to check both what is being moved and where it
    goes, so a move can never widen the mover's own access."""
    if user.role.is_super_admin:
        return True
    return resolve_folder_or_customer_capability(
        session, user, folder_id=folder_id, customer_id=customer_id, capability=capability
    )


def _capabilities_at(
    session: Session, user: User, *, folder_id: int | None, customer_id: int | None
) -> set[Capability]:
    return {
        capability
        for capability in Capability
        if resolve_folder_or_customer_capability(
            session, user, folder_id=folder_id, customer_id=customer_id, capability=capability
        )
    }


def move_keeps_access_within(
    session: Session,
    user: User,
    *,
    scope_type: ScopeType,
    scope_id: int,
    from_folder_id: int | None,
    from_customer_id: int | None,
    to_folder_id: int | None,
    to_customer_id: int | None,
) -> bool:
    """Whether moving a folder or source (`scope_type`/`scope_id`) from one
    place to another leaves the mover with no capability on it they didn't
    already have. With no grant of the mover's role on the moved item itself,
    it inherits whatever governs where it lands, so the capabilities the
    mover holds at the destination must all be held where it is now. With a
    grant of its own, that grant keeps governing it and the move changes
    nothing for the mover. Super admins hold everything."""
    if user.role.is_super_admin:
        return True
    if _find_grant(session, role_id=user.role_id, scope_type=scope_type, scope_id=scope_id):
        return True
    return _capabilities_at(
        session, user, folder_id=to_folder_id, customer_id=to_customer_id
    ) <= _capabilities_at(session, user, folder_id=from_folder_id, customer_id=from_customer_id)


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


@dataclass
class FolderVisibilityContext:
    """Precomputed, request-scoped state for `folder_path_boundary` below —
    built once per request (via `build_folder_visibility_context`), not
    once per source, so serializing a whole `GET /sources` list doesn't
    cost a folder/grant lookup per source per ancestor level."""

    is_super_admin: bool
    folders_by_id: dict[int, Folder] = field(default_factory=dict)
    source_grant_ids: set[int] = field(default_factory=set)
    folder_grant_ids: set[int] = field(default_factory=set)


def build_folder_visibility_context(session: Session, user: User) -> FolderVisibilityContext:
    folders_by_id = {f.id: f for f in session.exec(select(Folder)).all()}
    if user.role.is_super_admin:
        return FolderVisibilityContext(is_super_admin=True, folders_by_id=folders_by_id)

    grants = session.exec(select(RoleGrant).where(RoleGrant.role_id == user.role_id)).all()
    return FolderVisibilityContext(
        is_super_admin=False,
        folders_by_id=folders_by_id,
        source_grant_ids={g.scope_id for g in grants if g.scope_type == ScopeType.source},
        folder_grant_ids={g.scope_id for g in grants if g.scope_type == ScopeType.folder},
    )


def folder_path_boundary(ctx: FolderVisibilityContext, source: Source) -> int | None:
    """Returns the folder id at which a source's displayed folder ancestor
    chain (api/sources.py's `folder_path`) should stop walking upward, or
    None for "no boundary" (show the whole chain up to the top). A grant
    scoped to a specific folder or source authorizes visibility into that
    scope and everything beneath it, not its own ancestors — so revealing
    folder names above the grant's own scope would leak organizational
    structure a narrower grant was never meant to disclose (CLAUDE.md's
    per-folder/per-source RBAC scoping: "most specific scope wins"). A
    customer-level grant (or super-admin) already implies visibility into
    the customer's whole structure, so nothing is truncated there.

    Assumes `source` is already known to be visible to the context's user
    (e.g. via `visible_source_ids`) — it only determines which scope
    governs that visibility, it doesn't re-check the `view` capability
    itself. That's safe because `resolve_capability` never falls through
    to a broader scope once a more specific one has any grant recorded, so
    a source that's actually visible can't have an intervening ungranted
    scope between it and whichever scope this finds.
    """
    if ctx.is_super_admin:
        return None
    if source.id in ctx.source_grant_ids:
        return source.folder_id

    folder_id = source.folder_id
    while folder_id is not None:
        if folder_id in ctx.folder_grant_ids:
            return folder_id
        folder = ctx.folders_by_id.get(folder_id)
        folder_id = folder.parent_folder_id if folder else None

    return None


def delete_scope_grants(session: Session, scope_type: ScopeType, scope_id: int) -> None:
    """RoleGrant.scope_id has no FK (it can point at three tables), so the
    database won't clean these up itself when a customer/folder/source is
    deleted -- callers deleting one of those must call this in the same
    transaction."""
    for grant in session.exec(
        select(RoleGrant).where(RoleGrant.scope_type == scope_type, RoleGrant.scope_id == scope_id)
    ).all():
        session.delete(grant)


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


ROLE_BEYOND_AUTHORITY = "Not permitted: that role has privileges beyond your own"


def _actor_holds_at_scope(
    session: Session, actor: User, scope_type: ScopeType, scope_id: int, capability: Capability
) -> bool:
    """Whether `actor` holds `capability` at a customer, folder or source,
    by their own grant resolution: granted there, or at a scope above it
    that covers it, the most specific grant winning as always."""
    if scope_type == ScopeType.source:
        source = session.get(Source, scope_id)
        return source is None or resolve_capability(session, actor, source, capability)
    if scope_type == ScopeType.folder:
        folder = session.get(Folder, scope_id)
        if folder is None:
            return True
        return resolve_folder_or_customer_capability(
            session,
            actor,
            folder_id=folder.id,
            customer_id=folder.customer_id,
            capability=capability,
        )
    return resolve_folder_or_customer_capability(
        session, actor, folder_id=None, customer_id=scope_id, capability=capability
    )


def _scope_chain(session: Session, scope_type: ScopeType, scope_id: int) -> list[tuple]:
    """A scope and every scope above it, nearest first: a source, its folder
    chain and its customer; a folder, its parents and its customer; a
    customer alone. Scopes that no longer exist end the chain."""
    chain: list[tuple] = [(scope_type, scope_id)]
    folder_id: int | None = None
    customer_id: int | None = None
    if scope_type == ScopeType.source:
        source = session.get(Source, scope_id)
        if source is None:
            return chain
        folder_id, customer_id = source.folder_id, source.customer_id
    elif scope_type == ScopeType.folder:
        folder = session.get(Folder, scope_id)
        if folder is None:
            return chain
        folder_id, customer_id = folder.parent_folder_id, folder.customer_id
    while folder_id is not None:
        chain.append((ScopeType.folder, folder_id))
        folder = session.get(Folder, folder_id)
        folder_id = folder.parent_folder_id if folder else None
    if customer_id is not None:
        chain.append((ScopeType.customer, customer_id))
    return chain


def grant_within_authority(
    session: Session,
    actor: User,
    *,
    scope_type: ScopeType,
    scope_id: int,
    capabilities: list[Capability],
    role_id: int | None = None,
) -> bool:
    """Whether a grant of `capabilities` at one scope is within what `actor`
    holds there themselves: by their grant resolution at that scope, and in
    every narrower scope beneath it where the actor has a grant of their own
    (a folder or source the actor holds less in). A narrower scope where the
    role being changed (`role_id`) has its own grant, at it or between it and
    this scope, is governed by that grant instead, so it isn't compared here.
    A grant whose scope no longer exists gives nothing, so it is within
    anyone's authority."""
    if actor.role.is_super_admin:
        return True
    wanted = set(capabilities)
    if not all(
        _actor_holds_at_scope(session, actor, scope_type, scope_id, capability)
        for capability in wanted
    ):
        return False
    if not wanted:
        return True

    role_scopes: set[tuple] = set()
    if role_id is not None:
        role_scopes = {
            (g.scope_type, g.scope_id)
            for g in session.exec(select(RoleGrant).where(RoleGrant.role_id == role_id)).all()
        }
    target = (scope_type, scope_id)
    for own in session.exec(select(RoleGrant).where(RoleGrant.role_id == actor.role_id)).all():
        chain = _scope_chain(session, own.scope_type, own.scope_id)
        if target not in chain[1:]:
            continue
        if any(scope in role_scopes for scope in chain[: chain.index(target)]):
            continue
        if not wanted <= set(own.capabilities):
            return False
    return True


def role_within_authority(session: Session, actor: User, role: Role) -> bool:
    """Whether `actor` may assign, edit, or act on users holding `role`
    without that being a privilege escalation. A delegated admin
    (manage_users / manage_roles / manage_sso, short of super-admin) can
    only work with roles that are no more powerful than their own: never a
    super-admin role, never one carrying a global capability the actor
    doesn't hold themselves, and never one with a grant reaching beyond what
    the actor holds at that grant's scope (see grant_within_authority).
    Without this, any of those capabilities is a path to wider access --
    e.g. manage_users assigning itself the Super Admin role, resetting a
    super-admin's password, or assigning someone a role that sees customers
    the admin can't."""
    if actor.role.is_super_admin:
        return True
    if role.is_super_admin:
        return False
    if not set(role.global_capabilities) <= set(actor.role.global_capabilities):
        return False
    grants = session.exec(select(RoleGrant).where(RoleGrant.role_id == role.id)).all()
    return all(
        grant_within_authority(
            session,
            actor,
            scope_type=grant.scope_type,
            scope_id=grant.scope_id,
            capabilities=grant.capabilities,
            role_id=role.id,
        )
        for grant in grants
    )


def require_role_within_authority(session: Session, actor: User, role: Role) -> None:
    if not role_within_authority(session, actor, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ROLE_BEYOND_AUTHORITY)


def require_changed_role_within_authority(session: Session, actor: User, role: Role) -> None:
    """Check a role after a change to its grants, before that change is
    committed: the whole role, as it now stands, must still be within the
    actor's authority. Removing or narrowing one grant can widen what
    another grant reaches (a narrower grant of the role is what keeps the
    actor's own narrower grant from bounding a broader one above it), so
    checking only the grant being changed isn't enough. On refusal the
    change is rolled back."""
    session.flush()
    if not role_within_authority(session, actor, role):
        session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ROLE_BEYOND_AUTHORITY)


def require_grant_within_authority(
    session: Session,
    actor: User,
    *,
    scope_type: ScopeType,
    scope_id: int,
    capabilities: list[Capability],
    role_id: int | None = None,
) -> None:
    if not grant_within_authority(
        session,
        actor,
        scope_type=scope_type,
        scope_id=scope_id,
        capabilities=capabilities,
        role_id=role_id,
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ROLE_BEYOND_AUTHORITY)


OWN_ROLE_REFUSED = "You can't change your own role or the role you hold."


def require_not_own_role(actor: User, role: Role) -> None:
    """Only a super admin changes the role they hold: anyone else editing
    it would widen their own access."""
    if not actor.role.is_super_admin and role.id == actor.role_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=OWN_ROLE_REFUSED)


def require_capabilities_within_authority(
    actor: User, global_capabilities: list[GlobalCapability]
) -> None:
    if actor.role.is_super_admin:
        return
    if not set(global_capabilities) <= set(actor.role.global_capabilities):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not permitted: you can only grant global capabilities you hold yourself",
        )


def record_role_created(session: Session, *, actor_user_id: int | None, role: Role) -> None:
    record_audit_event(
        session,
        user_id=actor_user_id,
        action="role.create",
        target_type="role",
        target_id=role.id,
        metadata={"name": role.name},
    )


def record_role_grant_created(
    session: Session, *, actor_user_id: int | None, grant: RoleGrant
) -> None:
    record_audit_event(
        session,
        user_id=actor_user_id,
        action="role_grant.create",
        target_type="role_grant",
        target_id=grant.id,
        metadata={
            "role_id": grant.role_id,
            "scope_type": grant.scope_type,
            "scope_id": grant.scope_id,
        },
    )


def create_role(
    session: Session,
    *,
    actor_user_id: int | None,
    name: str,
    is_builtin: bool = False,
    is_super_admin: bool = False,
    global_capabilities: list[GlobalCapability] | None = None,
    commit: bool = True,
    audit: bool = True,
) -> Role:
    """With ``audit=False`` the caller records the event itself, with
    ``record_role_created``, once it has checked the change it is part of:
    a refused change then leaves no audit row and no log line behind."""
    role = Role(
        name=name,
        is_builtin=is_builtin,
        is_super_admin=is_super_admin,
        global_capabilities=global_capabilities or [],
    )
    session.add(role)
    session.flush()

    if audit:
        record_role_created(session, actor_user_id=actor_user_id, role=role)
    if commit:
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
    commit: bool = True,
    audit: bool = True,
) -> RoleGrant:
    """``audit=False`` as in ``create_role``, with ``record_role_grant_created``."""
    grant = RoleGrant(
        role_id=role_id,
        scope_type=scope_type,
        scope_id=scope_id,
        capabilities=capabilities,
    )
    session.add(grant)
    session.flush()

    if audit:
        record_role_grant_created(session, actor_user_id=actor_user_id, grant=grant)
    if commit:
        session.commit()
        session.refresh(grant)
    return grant
