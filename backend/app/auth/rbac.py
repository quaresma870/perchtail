from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from fastapi import Depends, HTTPException, status
from sqlmodel import Session, select

from app import request_context
from app.audit import record_audit_event
from app.auth.models import Capability, GlobalCapability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Folder, Source


class PermissionDenied(HTTPException):
    """A 403 that also says, for the audit log only, why it was refused
    (app/api/errors.py records every 403 as `access.denied`). The detail
    the caller sees stays neutral."""

    def __init__(self, detail, *, reason: str) -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        self.reason = reason


def forbidden(detail, *, reason: str, user: User | None = None) -> PermissionDenied:
    """The 403 to raise for a refused permission. `user`, when given, is
    who was refused, for the access.denied entry: needed when the check
    runs before (or without) the session cookie check that normally names
    them."""
    if user is not None:
        context = request_context.current()
        if context is not None and context.user_id is None:
            context.user_id = user.id
    return PermissionDenied(detail, reason=reason)


class ConcealedDenial(HTTPException):
    """A refusal answered as 404, as if the target didn't exist, so the
    answer doesn't reveal that it does (a customer or folder outside the
    caller's reach, a role beyond their authority, the audit log while it
    is turned off). Audited like a 403 (app/api/errors.py), with the
    reason; the caller only sees the neutral detail."""

    def __init__(self, detail, *, reason: str) -> None:
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
        self.reason = reason


def concealed(detail, *, reason: str, user: User | None = None) -> ConcealedDenial:
    """The 404 to raise for a refusal that mustn't reveal its target; `user`
    as in `forbidden`."""
    if user is not None:
        context = request_context.current()
        if context is not None and context.user_id is None:
            context.user_id = user.id
    return ConcealedDenial(detail, reason=reason)


def _find_grant(session: Session, *, role_id: int, scope_type: ScopeType, scope_id: int):
    return session.exec(
        select(RoleGrant).where(
            RoleGrant.role_id == role_id,
            RoleGrant.scope_type == scope_type,
            RoleGrant.scope_id == scope_id,
        )
    ).first()


# --- Grant resolution ------------------------------------------------------
#
# `resolve_access` is the one function that decides what a principal holds
# on a source (or at a folder or customer): `resolve_capability` and the
# access report (app/access_report.py) both use it. It reads grants and the
# folder tree through a `GrantLookup`, so the same decision runs against
# the database (one query at a time, `DbGrantLookup`) or against a snapshot
# loaded once (the access report's), and it returns where the decision came
# from (`AccessVia`) along with the capabilities.


class GrantLookup(Protocol):
    def find_grant(
        self, *, role_id: int, scope_type: ScopeType, scope_id: int
    ) -> RoleGrant | None: ...

    def folder_parent(self, folder_id: int) -> int | None: ...


class DbGrantLookup:
    """`GrantLookup` over the database, one query per step."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_grant(self, *, role_id: int, scope_type: ScopeType, scope_id: int):
        return _find_grant(self.session, role_id=role_id, scope_type=scope_type, scope_id=scope_id)

    def folder_parent(self, folder_id: int) -> int | None:
        return self.session.get(Folder, folder_id).parent_folder_id


PRINCIPAL_ROLE = "role"
# `AccessVia.scope_type` of a super admin's access, which no grant decides.
VIA_SUPER_ADMIN = "super_admin"


@dataclass(frozen=True)
class Principal:
    """Who access is resolved for. Today only a role (`PRINCIPAL_ROLE`):
    every user holding a role has exactly its access."""

    principal_type: str
    principal_id: int
    is_super_admin: bool = False


@dataclass(frozen=True)
class AccessVia:
    """What decided an access: the principal, and the scope of its grant
    that decided it, or `VIA_SUPER_ADMIN` with no scope id."""

    principal_type: str
    principal_id: int
    scope_type: str
    scope_id: int | None


@dataclass(frozen=True)
class AccessDecision:
    capabilities: frozenset[Capability]
    via: tuple[AccessVia, ...] = ()


NO_ACCESS = AccessDecision(capabilities=frozenset())


def role_principal(role: Role) -> Principal:
    return Principal(PRINCIPAL_ROLE, role.id, is_super_admin=role.is_super_admin)


def resolve_access(
    lookup: GrantLookup,
    principal: Principal,
    *,
    source_id: int | None = None,
    is_system: bool = False,
    folder_id: int | None,
    customer_id: int | None,
) -> AccessDecision:
    """What `principal` holds on a source (`source_id`, placed at `folder_id`
    and `customer_id`), or, with no `source_id`, at a folder or at the
    customer's root, per CLAUDE.md's "Access control (RBAC + SSO)" section:

    - no grant reaches a system source; only a super admin does;
    - a super admin holds everything;
    - otherwise the most specific grant of the principal decides alone: on
      the source, then its folder and each folder above it, nearest first,
      then the customer. A grant decides even when it holds nothing, so a
      narrower grant can take away what a broader one gives;
    - with no grant, nothing.

    A folder chain that loops back on itself (which the folders API never
    creates) holds nothing, rather than walking forever."""
    if principal.is_super_admin:
        return AccessDecision(
            capabilities=frozenset(Capability),
            via=(
                AccessVia(principal.principal_type, principal.principal_id, VIA_SUPER_ADMIN, None),
            ),
        )
    if is_system:
        return NO_ACCESS

    def decided(grant: RoleGrant) -> AccessDecision:
        held = grant.capabilities or []
        return AccessDecision(
            capabilities=frozenset(c for c in Capability if c in held),
            via=(
                AccessVia(
                    principal.principal_type,
                    principal.principal_id,
                    str(grant.scope_type),
                    grant.scope_id,
                ),
            ),
        )

    role_id = principal.principal_id
    if source_id is not None:
        grant = lookup.find_grant(role_id=role_id, scope_type=ScopeType.source, scope_id=source_id)
        if grant is not None:
            return decided(grant)

    walked: set[int] = set()
    while folder_id is not None:
        if folder_id in walked:
            return NO_ACCESS
        walked.add(folder_id)
        grant = lookup.find_grant(role_id=role_id, scope_type=ScopeType.folder, scope_id=folder_id)
        if grant is not None:
            return decided(grant)
        folder_id = lookup.folder_parent(folder_id)

    if customer_id is not None:
        grant = lookup.find_grant(
            role_id=role_id, scope_type=ScopeType.customer, scope_id=customer_id
        )
        if grant is not None:
            return decided(grant)

    return NO_ACCESS


def resolve_folder_or_customer_capability(
    session: Session,
    user: User,
    *,
    folder_id: int | None,
    customer_id: int | None,
    capability: Capability,
) -> bool:
    """Grant resolution at a folder (its chain, nearest first, then the
    customer) or at a customer's root, by the role's grants alone: the
    super-admin short-circuit is the caller's concern, not this helper's,
    so the role is resolved as an ordinary one here (see `resolve_access`)."""
    principal = Principal(PRINCIPAL_ROLE, user.role_id)
    decision = resolve_access(
        DbGrantLookup(session), principal, folder_id=folder_id, customer_id=customer_id
    )
    return capability in decision.capabilities


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
    decision = resolve_access(
        DbGrantLookup(session),
        role_principal(user.role),
        folder_id=folder_id,
        customer_id=customer_id,
    )
    return capability in decision.capabilities


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
    customer-scoped one; otherwise deny. Decided by `resolve_access`."""
    return capability in resolve_source_access(session, user, source).capabilities


def resolve_source_access(session: Session, user: User, source: Source) -> AccessDecision:
    """Everything `user` holds on `source`, and what decided it."""
    return resolve_access(
        DbGrantLookup(session),
        role_principal(user.role),
        source_id=source.id,
        is_system=source.is_system,
        folder_id=source.folder_id,
        customer_id=source.customer_id,
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


@dataclass(frozen=True)
class OrgReach:
    """The customers and folders a role's grants reach (see `org_reach`)."""

    customer_ids: frozenset[int]
    folder_ids: frozenset[int]


def org_reach(session: Session, user: User) -> OrgReach | None:
    """The customers and folders `user` may see in the organization's
    structure, or None for all of them (super admin). Customers and folder
    names are information of each customer, so a role only sees those its
    own grants reach:

    - a customer with a grant on it, on one of its folders or on one of its
      sources;
    - every folder of a customer with a customer grant;
    - a folder with a grant, and every folder below it;
    - the folder holding a source with a grant, and the folders above that
      one, for the path to it.

    Any grant counts, whatever its capabilities: this only decides what the
    lists show and what answers 404. What may be done there is still checked
    by grant resolution."""
    if user.role.is_super_admin:
        return None

    grants = session.exec(select(RoleGrant).where(RoleGrant.role_id == user.role_id)).all()
    if not grants:
        return OrgReach(customer_ids=frozenset(), folder_ids=frozenset())

    folders_by_id = {f.id: f for f in session.exec(select(Folder)).all()}
    children: dict[int, list[int]] = {}
    for folder in folders_by_id.values():
        if folder.parent_folder_id is not None:
            children.setdefault(folder.parent_folder_id, []).append(folder.id)

    customer_ids: set[int] = set()
    folder_ids: set[int] = set()

    def add_with_descendants(folder_id: int) -> None:
        pending = [folder_id]
        while pending:
            current = pending.pop()
            if current in folder_ids:
                continue
            folder_ids.add(current)
            pending.extend(children.get(current, []))

    def add_with_ancestors(folder_id: int | None) -> None:
        # The whole chain: a folder already reached by another grant (from
        # above it) doesn't mean the folders above it are.
        walked: set[int] = set()
        while folder_id is not None and folder_id not in walked:
            folder = folders_by_id.get(folder_id)
            if folder is None:
                return
            walked.add(folder_id)
            folder_ids.add(folder_id)
            folder_id = folder.parent_folder_id

    granted_customers = {g.scope_id for g in grants if g.scope_type == ScopeType.customer}
    for folder in folders_by_id.values():
        if folder.customer_id in granted_customers:
            folder_ids.add(folder.id)
    customer_ids |= granted_customers

    for grant in grants:
        if grant.scope_type == ScopeType.folder:
            folder = folders_by_id.get(grant.scope_id)
            if folder is not None:
                customer_ids.add(folder.customer_id)
                add_with_descendants(folder.id)
        elif grant.scope_type == ScopeType.source:
            source = session.get(Source, grant.scope_id)
            if source is None or source.is_system:
                continue
            if source.customer_id is not None:
                customer_ids.add(source.customer_id)
            add_with_ancestors(source.folder_id)

    return OrgReach(customer_ids=frozenset(customer_ids), folder_ids=frozenset(folder_ids))


def customer_in_reach(session: Session, user: User, customer_id: int) -> bool:
    reach = org_reach(session, user)
    return reach is None or customer_id in reach.customer_ids


def folder_in_reach(session: Session, user: User, folder_id: int) -> bool:
    reach = org_reach(session, user)
    return reach is None or folder_id in reach.folder_ids


def visible_customer_ids(session: Session, user: User) -> set[int] | None:
    """Returns the set of customer ids the user has any grant reaching
    (customer-scoped, or a folder/source grant somewhere underneath it), or
    None for "all" (super-admin); see `org_reach`."""
    reach = org_reach(session, user)
    return None if reach is None else set(reach.customer_ids)


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
            raise forbidden("Not permitted", reason=f"missing_capability:{capability}", user=user)
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
        raise forbidden(
            "Not permitted", reason=f"missing_global_capability:{capability}", user=user
        )

    return dependency


def require_any_global_capability(
    capabilities: tuple[GlobalCapability, ...], get_current_user: Callable[..., User]
):
    """Like `require_global_capability`, passing with any one of
    `capabilities`."""

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.is_super_admin:
            return user
        if any(capability in user.role.global_capabilities for capability in capabilities):
            return user
        names = "|".join(str(capability) for capability in capabilities)
        raise forbidden("Not permitted", reason=f"missing_global_capability:{names}", user=user)

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
        raise forbidden(ROLE_BEYOND_AUTHORITY, reason="role_beyond_authority", user=actor)


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
        raise forbidden(ROLE_BEYOND_AUTHORITY, reason="role_beyond_authority", user=actor)


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
        raise forbidden(ROLE_BEYOND_AUTHORITY, reason="grant_beyond_authority", user=actor)


OWN_ROLE_REFUSED = "You can't change your own role or the role you hold."


def require_not_own_role(actor: User, role: Role) -> None:
    """Only a super admin changes the role they hold: anyone else editing
    it would widen their own access."""
    if not actor.role.is_super_admin and role.id == actor.role_id:
        raise forbidden(OWN_ROLE_REFUSED, reason="own_role", user=actor)


def require_capabilities_within_authority(
    actor: User, global_capabilities: list[GlobalCapability]
) -> None:
    if actor.role.is_super_admin:
        return
    if not set(global_capabilities) <= set(actor.role.global_capabilities):
        raise forbidden(
            "Not permitted: you can only grant global capabilities you hold yourself",
            reason="global_capability_beyond_authority",
            user=actor,
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
