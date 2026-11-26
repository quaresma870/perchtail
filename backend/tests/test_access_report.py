import csv
import io
import json
import re
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app import access_report
from app.auth.models import AuthProviderType, Capability, GlobalCapability, Role, ScopeType, User
from app.auth.rbac import (
    DbGrantLookup,
    create_role_grant,
    resolve_access,
    resolve_capability,
    role_principal,
)
from app.db import configure_sqlite_connection
from app.models import Customer, Folder, Protocol, Source
from app.timeutils import utcnow
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select


def _add(session, row):
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _source(session, name, *, customer=None, folder=None, is_system=False):
    return _add(
        session,
        Source(
            name=name,
            customer_id=customer.id if customer else None,
            folder_id=folder.id if folder else None,
            protocol=Protocol.local if is_system else Protocol.ssh,
            host="host.example.com",
            base_path="/var/log",
            is_system=is_system,
        ),
    )


def _role(session, name, *, is_super_admin=False, global_capabilities=None):
    return _add(
        session,
        Role(
            name=name,
            is_super_admin=is_super_admin,
            global_capabilities=global_capabilities or [],
        ),
    )


def _grant(session, role, scope_type, scope, capabilities):
    create_role_grant(
        session,
        actor_user_id=None,
        role_id=role.id,
        scope_type=scope_type,
        scope_id=scope.id,
        capabilities=capabilities,
    )


def _user(session, username, role, **fields):
    fields.setdefault("last_login_at", utcnow())
    return _add(session, User(username=username, role_id=role.id, **fields))


@pytest.fixture()
def org(session):
    """Two customers; Acme with three nested folders, Beta with one; sources
    at every level and without a folder; the system source; roles whose
    grants widen and narrow (an empty folder grant included), one with no
    grants, a super admin and two delegated admins; and an inactive user."""
    o = SimpleNamespace()
    o.acme = _add(session, Customer(name="Acme"))
    o.beta = _add(session, Customer(name="Beta"))
    o.top = _add(session, Folder(name="Acme-Top", customer_id=o.acme.id))
    o.mid = _add(session, Folder(name="Acme-Mid", customer_id=o.acme.id, parent_folder_id=o.top.id))
    o.leaf = _add(
        session, Folder(name="Acme-Leaf", customer_id=o.acme.id, parent_folder_id=o.mid.id)
    )
    o.ops = _add(session, Folder(name="Beta-Ops", customer_id=o.beta.id))

    o.s_acme_root = _source(session, "acme-root", customer=o.acme)
    o.s_top = _source(session, "acme-top", customer=o.acme, folder=o.top)
    o.s_mid = _source(session, "acme-mid", customer=o.acme, folder=o.mid)
    o.s_leaf = _source(session, "acme-leaf", customer=o.acme, folder=o.leaf)
    o.s_leaf2 = _source(session, "acme-leaf2", customer=o.acme, folder=o.leaf)
    o.s_beta_root = _source(session, "beta-root", customer=o.beta)
    o.s_ops = _source(session, "beta-ops", customer=o.beta, folder=o.ops)
    o.s_system = _source(session, "PerchTail logs", is_system=True)
    o.sources = [
        o.s_acme_root,
        o.s_top,
        o.s_mid,
        o.s_leaf,
        o.s_leaf2,
        o.s_beta_root,
        o.s_ops,
        o.s_system,
    ]

    o.r_super = _role(session, "Super", is_super_admin=True)
    o.r_narrow = _role(session, "Narrow")
    _grant(session, o.r_narrow, ScopeType.customer, o.acme, [Capability.view, Capability.download])
    _grant(session, o.r_narrow, ScopeType.folder, o.mid, [])
    _grant(
        session, o.r_narrow, ScopeType.source, o.s_leaf2, [Capability.view, Capability.manage_rules]
    )
    o.r_folderish = _role(session, "Folderish")
    _grant(session, o.r_folderish, ScopeType.folder, o.top, [Capability.view])
    _grant(session, o.r_folderish, ScopeType.source, o.s_ops, [Capability.download])
    o.r_everything = _role(session, "Everything")
    _grant(session, o.r_everything, ScopeType.customer, o.acme, [Capability.view])
    _grant(session, o.r_everything, ScopeType.customer, o.beta, [Capability.view])
    o.r_nothing = _role(session, "Nothing")
    o.r_folder_admin = _role(
        session, "FolderAdmin", global_capabilities=[GlobalCapability.view_access_report]
    )
    _grant(
        session,
        o.r_folder_admin,
        ScopeType.folder,
        o.mid,
        [Capability.view, Capability.manage_source],
    )
    o.r_customer_admin = _role(
        session,
        "CustomerAdmin",
        global_capabilities=[GlobalCapability.view_access_report, GlobalCapability.manage_users],
    )
    _grant(session, o.r_customer_admin, ScopeType.customer, o.beta, [Capability.view])

    o.u_super = _user(session, "root", o.r_super, mfa_enabled=True)
    o.u_narrow = _user(session, "narrow", o.r_narrow)
    o.u_folderish = _user(
        session,
        "folderish",
        o.r_folderish,
        auth_provider=AuthProviderType.oidc,
        sso_role_group="log-readers",
    )
    o.u_everything = _user(session, "everything", o.r_everything, mfa_enabled=True)
    o.u_nothing = _user(session, "nothing", o.r_nothing, last_login_at=None)
    o.u_folder_admin = _user(session, "folder-admin", o.r_folder_admin, mfa_enabled=True)
    o.u_customer_admin = _user(session, "customer-admin", o.r_customer_admin)
    o.u_gone = _user(session, "gone", o.r_narrow, active=False)
    o.active_users = [
        o.u_super,
        o.u_narrow,
        o.u_folderish,
        o.u_everything,
        o.u_nothing,
        o.u_folder_admin,
        o.u_customer_admin,
    ]
    return o


def _caps(report, username, source_id):
    for user in report.users:
        if user.username == username:
            for entry in user.access:
                if entry.place.source_id == source_id:
                    return set(entry.capabilities)
            return set()
    return None


def test_report_lists_a_capability_exactly_when_resolve_capability_gives_it(session, org):
    report = access_report.build_report(session, org.u_super)

    assert {u.username for u in report.users} == {u.username for u in org.active_users}
    assert {p.source_id for p in report.sources} == {s.id for s in org.sources}
    for user in org.active_users:
        for source in org.sources:
            listed = _caps(report, user.username, source.id)
            for capability in Capability:
                assert (capability in listed) == resolve_capability(
                    session, user, source, capability
                ), (user.username, source.name, capability)


@pytest.mark.parametrize("generator", ["u_folder_admin", "u_customer_admin"])
def test_a_delegated_report_matches_resolution_within_its_scope(session, org, generator):
    admin = getattr(org, generator)
    report = access_report.build_report(session, admin)

    in_scope = {
        s.id
        for s in org.sources
        if any(resolve_capability(session, admin, s, c) for c in Capability)
    }
    assert report.scope == access_report.SCOPE_DELEGATED
    assert {p.source_id for p in report.sources} == in_scope
    for user in report.users:
        model = session.get(User, user.id)
        for source in org.sources:
            listed = _caps(report, user.username, source.id)
            for capability in Capability:
                expected = source.id in in_scope and resolve_capability(
                    session, model, source, capability
                )
                assert (capability in listed) == expected, (user.username, source.name)


def test_the_in_memory_snapshot_decides_exactly_as_the_database(session, org):
    snapshot = access_report.load_snapshot(session)
    db = DbGrantLookup(session)
    for role in session.exec(select(Role)).all():
        for source in org.sources:
            kwargs = dict(
                source_id=source.id,
                is_system=source.is_system,
                folder_id=source.folder_id,
                customer_id=source.customer_id,
            )
            assert resolve_access(snapshot, role_principal(role), **kwargs) == resolve_access(
                db, role_principal(role), **kwargs
            )


def test_via_names_the_grant_that_decided(session, org):
    report = access_report.build_report(session, org.u_super)
    narrow = next(u for u in report.users if u.username == "narrow")
    by_source = {e.place.source_id: e for e in narrow.access}

    (via,) = by_source[org.s_leaf2.id].via
    assert (via.principal_type, via.principal_id, via.principal_name) == (
        "role",
        org.r_narrow.id,
        "Narrow",
    )
    assert (via.scope_type, via.scope_id) == ("source", org.s_leaf2.id)
    assert via.scope_name == "acme-leaf2"
    (via,) = by_source[org.s_top.id].via
    assert (via.scope_type, via.scope_id, via.scope_name) == ("customer", org.acme.id, "Acme")
    # The empty folder grant takes away what the customer grant gives.
    assert org.s_mid.id not in by_source and org.s_leaf.id not in by_source

    root = next(u for u in report.users if u.username == "root")
    system_entry = next(e for e in root.access if e.place.source_id == org.s_system.id)
    assert [(v.scope_type, v.scope_id) for v in system_entry.via] == [("super_admin", None)]


def test_a_folder_admin_sees_only_their_scope_and_nothing_above_it(session, org):
    report = access_report.build_report(session, org.u_folder_admin)

    assert {p.source_id for p in report.sources} == {org.s_mid.id, org.s_leaf.id, org.s_leaf2.id}
    # Who reaches those sources, whatever their own privileges; not the
    # users without access there (no manage_users).
    assert {u.username for u in report.users} == {
        "root",
        "narrow",
        "folderish",
        "everything",
        "folder-admin",
    }
    leaf = next(p for p in report.sources if p.source_id == org.s_leaf.id)
    assert (leaf.customer_name, leaf.folder_path, leaf.folder_path_truncated) == (
        "Acme",
        ("Acme-Mid", "Acme-Leaf"),
        True,
    )
    assert leaf.path_text() == "Acme / … / Acme-Mid / Acme-Leaf"

    folderish = next(u for u in report.users if u.username == "folderish")
    (via,) = folderish.access[0].via
    assert (via.scope_type, via.scope_id, via.scope_name, via.above_your_scope) == (
        "folder",
        None,
        None,
        True,
    )
    everything = next(u for u in report.users if u.username == "everything")
    (via,) = everything.access[0].via
    assert (via.scope_type, via.scope_id, via.above_your_scope) == ("customer", None, True)

    text = json.dumps(access_report.to_json(report, access_report.VIEW_USERS))
    text += json.dumps(access_report.to_json(report, access_report.VIEW_SOURCES))
    for hidden in ("Acme-Top", "Beta", "beta-", "acme-root", "acme-top", "PerchTail logs"):
        assert hidden not in text
    for user in report.users:
        for entry in user.access:
            for via in entry.via:
                assert not (via.scope_type == "folder" and via.scope_id == org.top.id)
                assert not (via.scope_type == "customer" and via.scope_id is not None)


def test_a_customer_admin_with_manage_users_also_sees_users_without_access(session, org):
    report = access_report.build_report(session, org.u_customer_admin)

    assert {p.source_id for p in report.sources} == {org.s_beta_root.id, org.s_ops.id}
    assert {u.username for u in report.users} == {u.username for u in org.active_users}
    nothing = next(u for u in report.users if u.username == "nothing")
    assert nothing.access == []
    folderish = next(u for u in report.users if u.username == "folderish")
    # Only the access inside the customer admin's scope.
    assert [e.place.source_id for e in folderish.access] == [org.s_ops.id]
    (via,) = folderish.access[0].via
    assert (via.scope_type, via.scope_name) == ("source", "beta-ops")
    everything = next(u for u in report.users if u.username == "everything")
    (via,) = everything.access[0].via
    assert (via.scope_type, via.scope_id, via.scope_name) == ("customer", org.beta.id, "Beta")
    ops = next(p for p in report.sources if p.source_id == org.s_ops.id)
    assert (ops.folder_path, ops.folder_path_truncated) == (("Beta-Ops",), False)


def test_flags(session, org):
    long_ago = utcnow() - timedelta(days=100)
    org.u_narrow.last_login_at = long_ago
    org.u_narrow.must_change_password = True
    session.add(org.u_narrow)
    session.commit()

    report = access_report.build_report(session, org.u_super, inactive_days=90)
    flags = {u.username: set(u.flags) for u in report.users}

    assert flags["root"] == {"super_admin", "global_capabilities", "reaches_all_sources"}
    assert flags["narrow"] == {"local_without_mfa", "must_change_password", "inactive"}
    assert flags["folderish"] == set()
    assert flags["everything"] == {"reaches_all_sources"}
    assert flags["nothing"] == {"local_without_mfa", "inactive"}
    assert flags["folder-admin"] == {"global_capabilities"}

    report = access_report.build_report(session, org.u_super, inactive_days=120)
    assert "inactive" not in next(u for u in report.users if u.username == "narrow").flags


def test_reaches_all_sources_is_per_capability_with_the_structural_detail(session, org):
    report = access_report.build_report(session, org.u_super)
    users = {u.username: u for u in report.users}

    assert users["everything"].reaches_all_capabilities == (Capability.view,)
    assert users["everything"].customer_grant_on_every_customer is True
    assert users["root"].reaches_all_capabilities == tuple(Capability)
    assert users["narrow"].reaches_all_capabilities == ()
    assert users["narrow"].customer_grant_on_every_customer is False


def test_user_details(session, org):
    report = access_report.build_report(session, org.u_super)
    users = {u.username: u for u in report.users}

    assert (users["folderish"].auth_provider, users["folderish"].mfa) == ("oidc", "idp")
    assert users["folderish"].sso_role_group == "log-readers"
    assert (users["narrow"].mfa, users["everything"].mfa) == ("disabled", "enabled")
    assert users["root"].global_capabilities == tuple(str(c) for c in GlobalCapability)
    assert users["customer-admin"].global_capabilities == ("manage_users", "view_access_report")


def test_json_carries_when_by_whom_scope_and_parameters(session, org):
    report = access_report.build_report(session, org.u_super, inactive_days=30)
    body = access_report.to_json(report, access_report.VIEW_USERS)

    assert body["generated_at"].endswith("Z")
    assert body["generated_by"] == {"id": org.u_super.id, "username": "root"}
    assert (body["scope"], body["view"], body["parameters"]) == (
        "full",
        "users",
        {"inactive_days": 30},
    )
    assert "email" not in json.dumps(body)

    by_source = access_report.to_json(report, access_report.VIEW_SOURCES)
    ops = next(s for s in by_source["sources"] if s["source_id"] == org.s_ops.id)
    holders = {u["username"]: u for u in ops["users"]}
    assert holders["folderish"]["capabilities"] == ["download"]
    assert holders["folderish"]["via"][0]["scope_type"] == "source"


def test_csv_has_fixed_columns_and_neutralises_formulas(session, org):
    _user(session, "=cmd|' /C calc'!A0", org.r_everything, display_name="+SUM(A1)")
    report = access_report.build_report(session, org.u_super)

    rows = list(csv.reader(io.StringIO("".join(access_report.iter_csv(report, "users")))))
    assert tuple(rows[0]) == access_report.USERS_CSV_COLUMNS
    evil = [r for r in rows[1:] if "cmd" in r[2]]
    assert evil and all(r[2] == "'=cmd|' /C calc'!A0" and r[3] == "'+SUM(A1)" for r in evil)
    assert all(r[0] == rows[1][0] and r[1] == "root" for r in rows[1:])
    nothing = [r for r in rows[1:] if r[2] == "nothing"]
    assert len(nothing) == 1 and nothing[0][11:] == ["", "", "", "", ""]

    rows = list(csv.reader(io.StringIO("".join(access_report.iter_csv(report, "sources")))))
    assert tuple(rows[0]) == access_report.SOURCES_CSV_COLUMNS
    leaf2 = [r for r in rows[1:] if r[3] == "acme-leaf2" and r[5] == "narrow"]
    assert leaf2[0][4] == "Acme / Acme-Top / Acme-Mid / Acme-Leaf"
    assert leaf2[0][8] == "view; manage_rules"
    assert leaf2[0][9] == "role Narrow: source acme-leaf2"


@pytest.mark.parametrize("value", ["=1", "+1", "-1", "@x", "\tx", "\rx"])
def test_safe_cell_prefixes_formula_starts(value):
    assert access_report.safe_cell(value) == "'" + value


@pytest.mark.parametrize(
    "value",
    [
        "\nx",
        "\n=1",
        " =1",
        "  +1",
        " -1",
        " @x",
        " =1",
        "　=1",
        "＝1",
        "＋1",
        "－1",
        "＠x",
        " ＝1",
    ],
)
def test_safe_cell_prefixes_hidden_formula_starts(value):
    """A line feed, whitespace before a formula character, or a full-width
    formula character still reads as a formula in a spreadsheet."""
    assert access_report.safe_cell(value) == "'" + value


def test_safe_cell_leaves_ordinary_values():
    assert access_report.safe_cell("alice") == "alice"
    assert access_report.safe_cell(" alice") == " alice"
    assert access_report.safe_cell("Alice Smith-Jones") == "Alice Smith-Jones"
    assert access_report.safe_cell(None) == ""
    assert access_report.safe_cell(12) == "12"


def _count_queries(session, run):
    engine = session.get_bind()
    count = 0

    def before(*_):
        nonlocal count
        count += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        run()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return count


def test_query_count_does_not_grow_with_sources(session, org):
    small = _count_queries(session, lambda: access_report.build_report(session, org.u_folder_admin))
    for i in range(50):
        _source(session, f"extra-{i}", customer=org.acme, folder=org.leaf)
    large = _count_queries(session, lambda: access_report.build_report(session, org.u_folder_admin))
    assert large == small


def _file_engine(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", configure_sqlite_connection)
    return engine


def test_the_snapshot_is_one_read_transaction(tmp_path):
    """A source, its customer and a grant committed on another connection
    while the snapshot is being read must not end up half in the report:
    the grants are read after the roles, so without one read transaction
    the new grant would be in and the source it reaches would not."""
    path = tmp_path / "report.db"
    reader_engine = _file_engine(path)
    writer_engine = _file_engine(path)
    SQLModel.metadata.create_all(reader_engine)

    with Session(writer_engine) as setup:
        acme = _add(setup, Customer(name="Acme"))
        _source(setup, "acme-1", customer=acme)
        role = _role(setup, "ops")
        _grant(setup, role, ScopeType.customer, acme, [Capability.view])
        _user(setup, "ops-user", role)
        role_id = role.id

    written = []

    def write_elsewhere(_conn, _cursor, statement, *_):
        if written or not statement.lstrip().upper().startswith("SELECT"):
            return
        written.append(True)
        with Session(writer_engine) as writer:
            beta = _add(writer, Customer(name="Beta"))
            _source(writer, "beta-1", customer=beta)
            _grant(writer, writer.get(Role, role_id), ScopeType.customer, beta, [Capability.view])

    with Session(reader_engine) as reader:
        event.listen(reader_engine, "after_cursor_execute", write_elsewhere)
        try:
            snapshot = access_report.load_snapshot(reader)
        finally:
            event.remove(reader_engine, "after_cursor_execute", write_elsewhere)
        driver = reader.connection().connection.driver_connection
        assert not driver.in_transaction  # ended, so a later write starts afresh

        assert written
        assert set(snapshot.customers.values()) == {"Acme"}
        assert [source.name for source in snapshot.sources] == ["acme-1"]
        assert len(snapshot.grants) == 1

        reader.commit()
        fresh = access_report.load_snapshot(reader)
        assert set(fresh.customers.values()) == {"Acme", "Beta"}
        assert sorted(source.name for source in fresh.sources) == ["acme-1", "beta-1"]
        assert len(fresh.grants) == 2

    reader_engine.dispose()
    writer_engine.dispose()


def test_the_snapshot_keeps_a_transaction_the_session_already_has(tmp_path):
    engine = _file_engine(tmp_path / "report.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        acme = _add(session, Customer(name="Acme"))
        session.add(
            Source(
                name="pending",
                customer_id=acme.id,
                protocol=Protocol.ssh,
                host="h",
                base_path="/",
            )
        )

        snapshot = access_report.load_snapshot(session)
        assert [source.name for source in snapshot.sources] == ["pending"]
        session.commit()

    with Session(engine) as other:
        assert [source.name for source in other.exec(select(Source)).all()] == ["pending"]
    engine.dispose()


@pytest.mark.postgres_only
def test_the_postgresql_snapshot_is_one_read_transaction(session):
    """As test_the_snapshot_is_one_read_transaction, on PostgreSQL: a
    customer, source and grant committed by another connection after the
    first read (the roles) must not end up half in the report."""
    acme = _add(session, Customer(name="Acme"))
    _source(session, "acme-1", customer=acme)
    role = _role(session, "ops")
    _grant(session, role, ScopeType.customer, acme, [Capability.view])
    _user(session, "ops-user", role)
    role_id = role.id
    session.commit()

    engine = session.get_bind()
    writer_engine = create_engine(engine.url)
    written = []

    def write_elsewhere(_conn, _cursor, statement, *_):
        # After the first read of the snapshot, the roles.
        if written or not re.search(r"\bFROM role\b", statement):
            return
        written.append(True)
        with Session(writer_engine) as writer:
            beta = _add(writer, Customer(name="Beta"))
            _source(writer, "beta-1", customer=beta)
            _grant(writer, writer.get(Role, role_id), ScopeType.customer, beta, [Capability.view])

    event.listen(engine, "after_cursor_execute", write_elsewhere)
    try:
        snapshot = access_report.load_snapshot(session)
    finally:
        event.remove(engine, "after_cursor_execute", write_elsewhere)
        writer_engine.dispose()

    assert written
    assert set(snapshot.customers.values()) == {"Acme"}
    assert [source.name for source in snapshot.sources] == ["acme-1"]
    assert len(snapshot.grants) == 1
    assert snapshot.roles[role_id].name == "ops"  # read, though no longer in a session

    fresh = access_report.load_snapshot(session)
    assert set(fresh.customers.values()) == {"Acme", "Beta"}
    assert sorted(source.name for source in fresh.sources) == ["acme-1", "beta-1"]
    assert len(fresh.grants) == 2


@pytest.mark.postgres_only
def test_the_postgresql_snapshot_is_repeatable_read_and_read_only(session, monkeypatch):
    seen = {}
    original = access_report._read_snapshot

    def read_snapshot(reader):
        seen["isolation"] = reader.execute(text("SHOW transaction_isolation")).scalar()
        seen["read_only"] = reader.execute(text("SHOW transaction_read_only")).scalar()
        return original(reader)

    monkeypatch.setattr(access_report, "_read_snapshot", read_snapshot)
    access_report.load_snapshot(session)
    assert seen == {"isolation": "repeatable read", "read_only": "on"}


@pytest.mark.postgres_only
def test_the_postgresql_snapshot_sees_what_the_session_wrote(session):
    """A session that wrote something keeps its own transaction for the
    reads, so the report sees what it hasn't committed yet."""
    acme = _add(session, Customer(name="Acme"))
    session.add(
        Source(name="pending", customer_id=acme.id, protocol=Protocol.ssh, host="h", base_path="/")
    )

    snapshot = access_report.load_snapshot(session)
    assert [source.name for source in snapshot.sources] == ["pending"]
    session.rollback()


def test_two_thousand_sources_and_fifty_users_stay_fast(session):
    customers = [Customer(name=f"customer-{i}") for i in range(20)]
    session.add_all(customers)
    session.commit()
    folders = []
    for customer in customers:
        top = Folder(name=f"{customer.name}-top", customer_id=customer.id)
        session.add(top)
        session.flush()
        sub = Folder(name=f"{customer.name}-sub", customer_id=customer.id, parent_folder_id=top.id)
        session.add(sub)
        session.flush()
        folders.append((customer, top, sub))
    session.add_all(
        Source(
            name=f"source-{i}",
            customer_id=folders[i % 20][0].id,
            folder_id=(None, folders[i % 20][1].id, folders[i % 20][2].id)[i % 3],
            protocol=Protocol.ssh,
            host="h",
            base_path="/",
        )
        for i in range(2000)
    )
    session.commit()
    roles = [_role(session, f"role-{i}") for i in range(10)]
    for i, role in enumerate(roles):
        customer, top, sub = folders[i]
        _grant(session, role, ScopeType.customer, customer, [Capability.view])
        _grant(session, role, ScopeType.folder, sub, [Capability.download])
    admin = _user(session, "admin", _role(session, "admin", is_super_admin=True))
    for i in range(49):
        _user(session, f"user-{i}", roles[i % 10])

    started = time.monotonic()
    report = access_report.build_report(session, admin)
    "".join(access_report.iter_csv(report, "users"))
    elapsed = time.monotonic() - started

    assert len(report.users) == 50
    assert elapsed < 5, elapsed
