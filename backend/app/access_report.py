"""The access report (#384): who holds what, where, for periodic access
reviews.

Built from the same grant resolution every request goes through
(`app.auth.rbac.resolve_access`), run against a snapshot of roles, grants,
customers, folders, sources and active users loaded once, so the report is
what PerchTail actually enforces and its query count doesn't grow with the
number of sources. Access depends only on a user's role today, so it is
resolved once per role and source and then listed for each user holding
that role.

What the generator sees (need-to-know):

- a super admin sees every active user and every source, the system log
  source included (scope `full`);
- anyone else holding `view_access_report` (scope `delegated`) sees only the
  sources where they hold a capability themselves, every active user who
  holds something on those sources (whatever their own privileges), and
  only that access. Folder names above the generator's own grant aren't
  shown (`rbac.folder_path_boundary`), nor the id or name of a grant scope
  above it. Users with no access in that scope are listed only to a
  generator who also holds `manage_users`, who sees every user in the
  Users list anyway.

Only what a review needs is read: usernames, display names, roles, sign-in
method, the IdP group that gave the role, last sign-in, MFA state and
capabilities. Never emails, hashes, secrets, tokens or file content."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.auth.models import (
    AuthProviderType,
    Capability,
    GlobalCapability,
    Role,
    RoleGrant,
    ScopeType,
    User,
)
from app.auth.rbac import (
    VIA_SUPER_ADMIN,
    AccessDecision,
    AccessVia,
    FolderVisibilityContext,
    folder_path_boundary,
    resolve_access,
    role_principal,
)
from app.models import Customer, Folder, Source
from app.timeutils import utcnow

DEFAULT_INACTIVE_DAYS = 90
MIN_INACTIVE_DAYS = 1
MAX_INACTIVE_DAYS = 3650

VIEW_USERS = "users"
VIEW_SOURCES = "sources"

SCOPE_FULL = "full"
SCOPE_DELEGATED = "delegated"

FLAG_SUPER_ADMIN = "super_admin"
FLAG_GLOBAL_CAPABILITIES = "global_capabilities"
FLAG_LOCAL_WITHOUT_MFA = "local_without_mfa"
FLAG_MUST_CHANGE_PASSWORD = "must_change_password"  # nosec B105 - a flag name
FLAG_INACTIVE = "inactive"
FLAG_REACHES_ALL_SOURCES = "reaches_all_sources"

MFA_ENABLED = "enabled"
MFA_DISABLED = "disabled"
MFA_IDP = "idp"


# --- Snapshot --------------------------------------------------------------


@dataclass(frozen=True)
class _FolderRow:
    id: int
    name: str
    customer_id: int
    parent_folder_id: int | None


@dataclass(frozen=True)
class _SourceRow:
    id: int
    name: str
    customer_id: int | None
    folder_id: int | None
    is_system: bool


@dataclass(frozen=True)
class _UserRow:
    id: int
    username: str
    display_name: str | None
    role_id: int
    auth_provider: AuthProviderType
    sso_role_group: str | None
    last_login_at: datetime | None
    mfa_enabled: bool
    must_change_password: bool


@dataclass
class Snapshot:
    """Everything the report reads, loaded in a fixed number of queries.
    Also a `rbac.GrantLookup`, so `resolve_access` runs against it."""

    roles: dict[int, Role]
    grants: dict[tuple[int, str, int], RoleGrant]
    customers: dict[int, str]
    folders: dict[int, _FolderRow]
    sources: list[_SourceRow]
    users: list[_UserRow]

    def find_grant(self, *, role_id: int, scope_type: ScopeType, scope_id: int):
        return self.grants.get((role_id, str(scope_type), scope_id))

    def folder_parent(self, folder_id: int) -> int | None:
        folder = self.folders.get(folder_id)
        return folder.parent_folder_id if folder is not None else None


@contextmanager
def _one_read_transaction(session: Session) -> Iterator[Session]:
    """Yields the session to run the reads with, in one database
    transaction, so they all see the same committed state: a grant or
    source committed by someone else halfway through can't end up half in
    the report.

    pysqlite only opens a transaction before a write, so each SELECT would
    otherwise see the latest commit. Here, and only here, a deferred `BEGIN`
    is sent on the session's own connection; SQLite in WAL mode then serves
    every read from the snapshot taken at the first one, without blocking
    writers. It is ended as soon as the reads are done, so the audit row the
    caller writes next starts a write transaction of its own instead of
    upgrading a stale snapshot. When the connection is already inside a
    transaction (the session wrote something), its reads already share one
    view and it is left alone.

    On PostgreSQL each statement of a READ COMMITTED transaction sees the
    latest commit, and the session's transaction has usually begun already
    (its isolation can no longer change). So, unless that transaction wrote
    something (it then keeps its own reads, which see its writes), the reads
    run on a connection of their own from the pool, in a REPEATABLE READ
    READ ONLY transaction: one snapshot for all of them, taken at the first,
    without blocking writers, rolled back once they are done
    (`_postgres_read_view`)."""
    session.flush()  # pending writes go out now, not inside the read transaction
    connection = session.connection()
    if connection.dialect.name == "postgresql":
        with _postgres_read_view(session, connection) as reader:
            yield reader
        return
    if connection.dialect.name != "sqlite":
        yield session
        return
    driver = connection.connection.driver_connection
    if driver.in_transaction:
        yield session
        return
    connection.exec_driver_sql("BEGIN")
    try:
        yield session
    finally:
        if driver.in_transaction:
            connection.exec_driver_sql("COMMIT")


@contextmanager
def _postgres_read_view(session: Session, connection) -> Iterator[Session]:
    """See _one_read_transaction. The rows come back detached from the
    reading session (closed, not committed, so their loaded columns stay
    as read); the report only reads their columns."""
    wrote = connection.exec_driver_sql(
        "SELECT pg_current_xact_id_if_assigned() IS NOT NULL"
    ).scalar()
    if wrote:
        yield session
        return
    with connection.engine.connect() as own:
        own = own.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        with Session(bind=own) as reader:
            yield reader


def load_snapshot(session: Session) -> Snapshot:
    """Roles, grants, customers, folders, sources and active users, read in
    one read transaction (six queries, whatever their size), so the report
    is one consistent view of access. Only the columns the report needs are
    read."""
    with _one_read_transaction(session) as reader:
        return _read_snapshot(reader)


def _read_snapshot(session: Session) -> Snapshot:
    roles = {role.id: role for role in session.exec(select(Role)).all()}
    grants = {
        (grant.role_id, str(grant.scope_type), grant.scope_id): grant
        for grant in session.exec(select(RoleGrant)).all()
    }
    customers = {
        row_id: name for row_id, name in session.exec(select(Customer.id, Customer.name)).all()
    }
    folders = {
        row[0]: _FolderRow(*row)
        for row in session.exec(
            select(Folder.id, Folder.name, Folder.customer_id, Folder.parent_folder_id)
        ).all()
    }
    sources = [
        _SourceRow(*row)
        for row in session.exec(
            select(Source.id, Source.name, Source.customer_id, Source.folder_id, Source.is_system)
        ).all()
    ]
    users = [
        _UserRow(*row)
        for row in session.exec(
            select(
                User.id,
                User.username,
                User.display_name,
                User.role_id,
                User.auth_provider,
                User.sso_role_group,
                User.last_login_at,
                User.mfa_enabled,
                User.must_change_password,
            ).where(User.active.is_(True))
        ).all()
    ]
    return Snapshot(
        roles=roles,
        grants=grants,
        customers=customers,
        folders=folders,
        sources=sources,
        users=users,
    )


# --- Report ----------------------------------------------------------------


@dataclass(frozen=True)
class ViaEntry:
    """What decided one access, as the generator may see it. `scope_type`
    is `customer`, `folder`, `source` or `super_admin`. A scope above the
    generator's own boundary keeps its type, with no id or name, and
    `above_your_scope` set."""

    principal_type: str
    principal_id: int
    principal_name: str
    scope_type: str
    scope_id: int | None
    scope_name: str | None
    above_your_scope: bool = False

    def as_dict(self) -> dict:
        return {
            "principal_type": self.principal_type,
            "principal_id": self.principal_id,
            "principal_name": self.principal_name,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "scope_name": self.scope_name,
            "above_your_scope": self.above_your_scope,
        }


@dataclass(frozen=True)
class SourcePlace:
    """A source and where it sits, cut at the generator's boundary."""

    source_id: int
    source_name: str
    is_system: bool
    customer_id: int | None
    customer_name: str | None
    folder_path: tuple[str, ...]
    folder_path_truncated: bool

    def path_text(self) -> str:
        parts: list[str] = []
        if self.customer_name is not None:
            parts.append(self.customer_name)
        if self.folder_path_truncated:
            parts.append("…")
        parts.extend(self.folder_path)
        return " / ".join(parts)


@dataclass(frozen=True)
class AccessEntry:
    place: SourcePlace
    capabilities: tuple[Capability, ...]
    via: tuple[ViaEntry, ...]


@dataclass
class UserEntry:
    id: int
    username: str
    display_name: str | None
    role_id: int
    role_name: str
    auth_provider: str
    sso_role_group: str | None
    last_login_at: datetime | None
    mfa: str
    global_capabilities: tuple[str, ...]
    flags: tuple[str, ...]
    reaches_all_capabilities: tuple[Capability, ...]
    customer_grant_on_every_customer: bool
    access: list[AccessEntry] = field(default_factory=list)


@dataclass
class AccessReport:
    generated_at: datetime
    generated_by_id: int
    generated_by_username: str
    scope: str
    inactive_days: int
    users: list[UserEntry]
    sources: list[SourcePlace]

    @property
    def row_count(self) -> int:
        return sum(len(user.access) for user in self.users)


def _ordered(capabilities) -> tuple[Capability, ...]:
    return tuple(c for c in Capability if c in capabilities)


def _effective_global_capabilities(role: Role) -> tuple[str, ...]:
    held = set(GlobalCapability) if role.is_super_admin else set(role.global_capabilities or [])
    return tuple(str(c) for c in GlobalCapability if c in held)


class _Scope:
    """The generator's view of the organization: which sources the report
    covers, and how much of each source's place it may show."""

    def __init__(self, snapshot: Snapshot, generator_role: Role) -> None:
        self.snapshot = snapshot
        self.full = generator_role.is_super_admin
        self.principal = role_principal(generator_role)
        own = [g for (role_id, _, _), g in snapshot.grants.items() if role_id == generator_role.id]
        self.ctx = FolderVisibilityContext(
            is_super_admin=self.full,
            folders_by_id=snapshot.folders,  # type: ignore[arg-type]
            source_grant_ids={g.scope_id for g in own if g.scope_type == ScopeType.source},
            folder_grant_ids={g.scope_id for g in own if g.scope_type == ScopeType.folder},
        )
        self.sources = [
            source
            for source in snapshot.sources
            if self.full or self._generator_access(source).capabilities
        ]
        self._places = {source.id: self._place(source) for source in self.sources}
        # The customers whose whole tree the generator sees: all of them for
        # a super admin, else those where their own customer grant decides.
        self.customer_ids = {
            customer_id
            for customer_id in snapshot.customers
            if self.full
            or resolve_access(
                snapshot, self.principal, folder_id=None, customer_id=customer_id
            ).capabilities
        }

    def _generator_access(self, source: _SourceRow) -> AccessDecision:
        return resolve_access(
            self.snapshot,
            self.principal,
            source_id=source.id,
            is_system=source.is_system,
            folder_id=source.folder_id,
            customer_id=source.customer_id,
        )

    def _boundary(self, source: _SourceRow) -> int | None:
        return folder_path_boundary(self.ctx, source)  # type: ignore[arg-type]

    def _chain(self, source: _SourceRow) -> list[_FolderRow]:
        """The source's folders up to the boundary, leaf first."""
        boundary = self._boundary(source)
        chain: list[_FolderRow] = []
        seen: set[int] = set()
        folder_id = source.folder_id
        while folder_id is not None and folder_id not in seen:
            seen.add(folder_id)
            folder = self.snapshot.folders.get(folder_id)
            if folder is None:
                break
            chain.append(folder)
            if folder.id == boundary:
                break
            folder_id = folder.parent_folder_id
        return chain

    def _place(self, source: _SourceRow) -> SourcePlace:
        chain = self._chain(source)
        truncated = bool(chain) and chain[-1].parent_folder_id is not None
        return SourcePlace(
            source_id=source.id,
            source_name=source.name,
            is_system=source.is_system,
            customer_id=source.customer_id,
            customer_name=self.snapshot.customers.get(source.customer_id),
            folder_path=tuple(folder.name for folder in reversed(chain)),
            folder_path_truncated=truncated,
        )

    def place(self, source: _SourceRow) -> SourcePlace:
        return self._places[source.id]

    def via(self, source: _SourceRow, via: AccessVia) -> ViaEntry:
        role = self.snapshot.roles.get(via.principal_id)
        principal_name = role.name if role is not None else ""
        scope_id: int | None = via.scope_id
        scope_name: str | None = None
        above = False
        if via.scope_type == VIA_SUPER_ADMIN:
            scope_id = None
        elif via.scope_type == ScopeType.source:
            scope_name = source.name
        elif via.scope_type == ScopeType.folder:
            visible = self.full or any(f.id == via.scope_id for f in self._chain(source))
            if visible:
                folder = self.snapshot.folders.get(via.scope_id)
                scope_name = folder.name if folder is not None else None
            else:
                scope_id, above = None, True
        elif via.scope_type == ScopeType.customer:
            if self.full or self._boundary(source) is None:
                scope_name = self.snapshot.customers.get(via.scope_id)
            else:
                scope_id, above = None, True
        return ViaEntry(
            principal_type=via.principal_type,
            principal_id=via.principal_id,
            principal_name=principal_name,
            scope_type=via.scope_type,
            scope_id=scope_id,
            scope_name=scope_name,
            above_your_scope=above,
        )


def build_report(
    session: Session,
    generator: User,
    *,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    now: datetime | None = None,
) -> AccessReport:
    """The access report as `generator` may see it (see the module
    docstring). Reads a snapshot (`load_snapshot`); doesn't write."""
    now = now or utcnow()
    snapshot = load_snapshot(session)
    generator_role = snapshot.roles[generator.role_id]
    scope = _Scope(snapshot, generator_role)

    access_cache: dict[tuple[int, int], AccessDecision] = {}

    def role_access(role: Role, source: _SourceRow) -> AccessDecision:
        key = (role.id, source.id)
        decision = access_cache.get(key)
        if decision is None:
            decision = resolve_access(
                snapshot,
                role_principal(role),
                source_id=source.id,
                is_system=source.is_system,
                folder_id=source.folder_id,
                customer_id=source.customer_id,
            )
            access_cache[key] = decision
        return decision

    entry_cache: dict[tuple[int, int], AccessEntry | None] = {}

    def role_entry(role: Role, source: _SourceRow) -> AccessEntry | None:
        """The report line of one role on one source, None when it holds
        nothing there. The same for every user holding the role."""
        key = (role.id, source.id)
        if key not in entry_cache:
            decision = role_access(role, source)
            entry_cache[key] = (
                AccessEntry(
                    place=scope.place(source),
                    capabilities=_ordered(decision.capabilities),
                    via=tuple(scope.via(source, via) for via in decision.via),
                )
                if decision.capabilities
                else None
            )
        return entry_cache[key]

    ordered_sources = sorted(scope.sources, key=lambda source: _place_key(scope.place(source)))
    non_system = [source for source in ordered_sources if not source.is_system]
    lists_everyone = scope.full or (
        GlobalCapability.manage_users in (generator_role.global_capabilities or [])
    )
    inactive_before = now - timedelta(days=inactive_days)

    role_cache: dict[int, tuple[list[AccessEntry], set[Capability], bool]] = {}

    def role_summary(role: Role) -> tuple[list[AccessEntry], set[Capability], bool]:
        """A role's report lines, the capabilities it holds on every source
        in the report, and whether a customer grant of it covers every
        customer the generator sees whole."""
        if role.id not in role_cache:
            access = [
                entry
                for source in ordered_sources
                if (entry := role_entry(role, source)) is not None
            ]
            everywhere: set[Capability] = set(Capability) if non_system else set()
            for source in non_system:
                everywhere &= role_access(role, source).capabilities
                if not everywhere:
                    break
            structural = bool(scope.customer_ids) and (
                role.is_super_admin
                or all(
                    _customer_grant_holds(snapshot, role, customer_id)
                    for customer_id in scope.customer_ids
                )
            )
            role_cache[role.id] = (access, everywhere, structural)
        return role_cache[role.id]

    users: list[UserEntry] = []
    for row in snapshot.users:
        role = snapshot.roles.get(row.role_id)
        if role is None:
            continue
        access, everywhere, structural = role_summary(role)
        if not access and not lists_everyone:
            continue

        global_capabilities = _effective_global_capabilities(role)
        is_local = row.auth_provider == AuthProviderType.local
        flags = []
        if role.is_super_admin:
            flags.append(FLAG_SUPER_ADMIN)
        if global_capabilities:
            flags.append(FLAG_GLOBAL_CAPABILITIES)
        if is_local and not row.mfa_enabled:
            flags.append(FLAG_LOCAL_WITHOUT_MFA)
        if row.must_change_password:
            flags.append(FLAG_MUST_CHANGE_PASSWORD)
        if row.last_login_at is None or row.last_login_at < inactive_before:
            flags.append(FLAG_INACTIVE)
        if everywhere:
            flags.append(FLAG_REACHES_ALL_SOURCES)

        if is_local:
            mfa = MFA_ENABLED if row.mfa_enabled else MFA_DISABLED
        else:
            mfa = MFA_IDP

        users.append(
            UserEntry(
                id=row.id,
                username=row.username,
                display_name=row.display_name,
                role_id=role.id,
                role_name=role.name,
                auth_provider=str(row.auth_provider),
                sso_role_group=None if is_local else row.sso_role_group,
                last_login_at=row.last_login_at,
                mfa=mfa,
                global_capabilities=global_capabilities,
                flags=tuple(flags),
                reaches_all_capabilities=_ordered(everywhere),
                customer_grant_on_every_customer=structural,
                access=list(access),
            )
        )

    users.sort(key=lambda user: (user.username.casefold(), user.id))
    places = [scope.place(source) for source in ordered_sources]

    return AccessReport(
        generated_at=now,
        generated_by_id=generator.id,
        generated_by_username=generator.username,
        scope=SCOPE_FULL if scope.full else SCOPE_DELEGATED,
        inactive_days=inactive_days,
        users=users,
        sources=places,
    )


def _customer_grant_holds(snapshot: Snapshot, role: Role, customer_id: int) -> bool:
    """Whether `role` has a grant holding something on the customer itself,
    which also reaches the sources added there later."""
    grant = snapshot.find_grant(
        role_id=role.id, scope_type=ScopeType.customer, scope_id=customer_id
    )
    return grant is not None and bool(grant.capabilities)


def _place_key(place: SourcePlace) -> tuple:
    return (
        not place.is_system,
        (place.customer_name or "").casefold(),
        tuple(name.casefold() for name in place.folder_path),
        place.source_name.casefold(),
        place.source_id,
    )


# --- Serialization ---------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """Naive UTC (app.timeutils) as ISO 8601 with a `Z`."""
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _header(report: AccessReport, view: str) -> dict:
    return {
        "generated_at": _iso(report.generated_at),
        "generated_by": {"id": report.generated_by_id, "username": report.generated_by_username},
        "scope": report.scope,
        "view": view,
        "parameters": {"inactive_days": report.inactive_days},
    }


def _place_dict(place: SourcePlace) -> dict:
    return {
        "source_id": place.source_id,
        "source_name": place.source_name,
        "is_system": place.is_system,
        "customer_id": place.customer_id,
        "customer_name": place.customer_name,
        "folder_path": list(place.folder_path),
        "folder_path_truncated": place.folder_path_truncated,
    }


def _user_dict(user: UserEntry) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": {"id": user.role_id, "name": user.role_name},
        "auth_provider": user.auth_provider,
        "sso_role_group": user.sso_role_group,
        "last_login_at": _iso(user.last_login_at),
        "mfa": user.mfa,
        "global_capabilities": list(user.global_capabilities),
        "flags": list(user.flags),
        "reaches_all_sources": {
            "capabilities": [str(c) for c in user.reaches_all_capabilities],
            "customer_grant_on_every_customer": user.customer_grant_on_every_customer,
        },
    }


def to_json(report: AccessReport, view: str) -> dict:
    body = _header(report, view)
    if view == VIEW_USERS:
        body["users"] = [
            {
                **_user_dict(user),
                "access": [
                    {
                        **_place_dict(entry.place),
                        "capabilities": [str(c) for c in entry.capabilities],
                        "via": [via.as_dict() for via in entry.via],
                    }
                    for entry in user.access
                ],
            }
            for user in report.users
        ]
    else:
        body["sources"] = [
            {
                **_place_dict(place),
                "users": [
                    {
                        "id": user.id,
                        "username": user.username,
                        "display_name": user.display_name,
                        "role": {"id": user.role_id, "name": user.role_name},
                        "capabilities": [str(c) for c in entry.capabilities],
                        "via": [via.as_dict() for via in entry.via],
                    }
                    for user, entry in users
                ],
            }
            for place, users in by_source(report)
        ]
    return body


_Holders = list[tuple[UserEntry, AccessEntry]]


def by_source(report: AccessReport) -> list[tuple[SourcePlace, _Holders]]:
    """Each source in the report with the users holding something on it."""
    holders: dict[int, _Holders] = {place.source_id: [] for place in report.sources}
    for user in report.users:
        for entry in user.access:
            holders[entry.place.source_id].append((user, entry))
    return [(place, holders[place.source_id]) for place in report.sources]


USERS_CSV_COLUMNS = (
    "generated_at",
    "generated_by",
    "username",
    "display_name",
    "role",
    "auth_provider",
    "sso_role_group",
    "last_login_at",
    "mfa",
    "global_capabilities",
    "flags",
    "source_id",
    "source",
    "path",
    "capabilities",
    "via",
)

SOURCES_CSV_COLUMNS = (
    "generated_at",
    "generated_by",
    "source_id",
    "source",
    "path",
    "username",
    "display_name",
    "role",
    "capabilities",
    "via",
)

# A cell starting with one of these, after any leading whitespace, is read as
# a formula by spreadsheet programs, which also take the full-width forms
# (U+FF1D, U+FF0B, U+FF0D, U+FF20) for the ASCII ones; usernames and display
# names can come from an identity provider.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "＝", "＋", "－", "＠")
# A cell starting with one of these is refused as is: a spreadsheet may drop
# it and read what follows as a formula.
_CONTROL_PREFIXES = ("\t", "\r", "\n")
# What a spreadsheet skips before it looks for a formula character.
_LEADING_SPACE = " \t\r\n 　"


def safe_cell(value) -> str:
    """A CSV cell that a spreadsheet won't run: one starting with a tab, a
    carriage return or a line feed, or with a formula character (ASCII or
    full-width) after any leading whitespace, gets a leading `'`."""
    text = "" if value is None else str(value)
    if text.startswith(_CONTROL_PREFIXES) or text.lstrip(_LEADING_SPACE).startswith(
        _FORMULA_PREFIXES
    ):
        return "'" + text
    return text


def _via_text(via: ViaEntry) -> str:
    if via.scope_type == VIA_SUPER_ADMIN:
        where = "super admin"
    elif via.above_your_scope:
        where = f"{via.scope_type} above your scope"
    else:
        where = f"{via.scope_type} {via.scope_name or via.scope_id}"
    return f"{via.principal_type} {via.principal_name}: {where}"


def _joined(values) -> str:
    return "; ".join(str(v) for v in values)


def _csv_line(cells) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow([safe_cell(cell) for cell in cells])
    return buffer.getvalue()


def iter_csv(report: AccessReport, view: str) -> Iterator[str]:
    """The report as CSV, one line at a time: one row per user and source
    (`users`) or per source and user (`sources`). A user without access, or
    a source nobody holds anything on, still gets one row with the other
    side empty. Every row carries when and by whom it was generated."""
    stamp = (_iso(report.generated_at), report.generated_by_username)
    if view == VIEW_USERS:
        yield _csv_line(USERS_CSV_COLUMNS)
        for user in report.users:
            person = (
                user.username,
                user.display_name,
                user.role_name,
                user.auth_provider,
                user.sso_role_group,
                _iso(user.last_login_at),
                user.mfa,
                _joined(user.global_capabilities),
                _joined(user.flags),
            )
            if not user.access:
                yield _csv_line((*stamp, *person, "", "", "", "", ""))
            for entry in user.access:
                yield _csv_line(
                    (
                        *stamp,
                        *person,
                        entry.place.source_id,
                        entry.place.source_name,
                        entry.place.path_text(),
                        _joined(entry.capabilities),
                        _joined(_via_text(via) for via in entry.via),
                    )
                )
        return

    yield _csv_line(SOURCES_CSV_COLUMNS)
    for place, holders in by_source(report):
        where = (place.source_id, place.source_name, place.path_text())
        if not holders:
            yield _csv_line((*stamp, *where, "", "", "", "", ""))
        for user, entry in holders:
            yield _csv_line(
                (
                    *stamp,
                    *where,
                    user.username,
                    user.display_name,
                    user.role_name,
                    _joined(entry.capabilities),
                    _joined(_via_text(via) for via in entry.via),
                )
            )
