import uuid
from datetime import timedelta

import pytest
from app.api.audit import router as audit_router
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, Role, User
from app.db import get_session
from app.timeutils import utcnow
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_user(session, *, is_super_admin=False, global_capabilities=None) -> User:
    role = Role(
        name=f"role-{is_super_admin}-{global_capabilities}-{uuid.uuid4().hex}",
        is_super_admin=is_super_admin,
        global_capabilities=global_capabilities or [],
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"user-{role.id}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(audit_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def _seed(session, viewer_id, *, count=1, action="source.create", target_type="source", **kw):
    entries = []
    for i in range(count):
        entries.append(
            record_audit_event(
                session,
                user_id=viewer_id,
                action=action,
                target_type=target_type,
                target_id=i,
                **kw,
            )
        )
    session.commit()
    return entries


def test_plain_user_cannot_read_audit_log(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.get("/audit")
    assert response.status_code == 403


def test_capability_holder_can_read_audit_log(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    _seed(session, user.id, count=3)
    client = client_for(user)

    response = client.get("/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert body["items"][0]["username"] == user.username


def test_super_admin_can_read_without_the_explicit_capability(session, client_for):
    admin = _make_user(session, is_super_admin=True)
    _seed(session, admin.id, count=1)
    client = client_for(admin)

    response = client.get("/audit")
    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_manage_system_settings_alone_does_not_grant_audit_read(session, client_for):
    # view_audit_log is deliberately its own capability, never implied by
    # another one -- see GlobalCapability's docstring in auth/models.py.
    user = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    client = client_for(user)

    response = client.get("/audit")
    assert response.status_code == 403


def test_pagination_limit_and_offset(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=5)
    client = client_for(user)

    response = client.get("/audit", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_filter_by_exact_action(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    _seed(session, user.id, count=1, action="user.login", target_type=None)
    client = client_for(user)

    response = client.get("/audit", params={"action": "user.login"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "user.login"


def test_filter_by_action_prefix_wildcard(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    _seed(session, user.id, count=1, action="source.delete", target_type="source")
    _seed(session, user.id, count=1, action="user.login", target_type=None)
    client = client_for(user)

    response = client.get("/audit", params={"action": "source.*"})
    body = response.json()
    assert body["total"] == 2
    assert {item["action"] for item in body["items"]} == {"source.create", "source.delete"}


def test_action_prefix_wildcard_does_not_treat_underscore_as_a_sql_wildcard(session, client_for):
    # "role_grant.*" strips to the prefix "role_grant." -- SQL LIKE treats a
    # literal "_" as a single-character wildcard unless escaped, which would
    # make this prefix also match an unrelated action like "roleXgrant.foo".
    # _action_condition's startswith(..., autoescape=True) must prevent that.
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="role_grant.create", target_type="role_grant")
    _seed(session, user.id, count=1, action="roleXgrant.create", target_type=None)
    client = client_for(user)

    response = client.get("/audit", params={"action": "role_grant.*"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "role_grant.create"


def test_filter_by_target_type(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="rule.create", target_type="rule")
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    client = client_for(user)

    response = client.get("/audit", params={"target_type": "rule"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["target_type"] == "rule"


def test_filter_by_user_id(session, client_for):
    user = _make_user(session, is_super_admin=True)
    other = _make_user(session)
    _seed(session, user.id, count=1)
    _seed(session, other.id, count=1)
    client = client_for(user)

    response = client.get("/audit", params={"user_id": other.id})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["user_id"] == other.id


def test_filter_by_date_range(session, client_for):
    user = _make_user(session, is_super_admin=True)
    old_entry = record_audit_event(session, user_id=user.id, action="source.create")
    session.commit()
    old_entry.timestamp = utcnow() - timedelta(days=10)
    session.add(old_entry)
    session.commit()

    _seed(session, user.id, count=1, action="source.update")
    client = client_for(user)

    since = (utcnow() - timedelta(days=1)).isoformat()
    response = client.get("/audit", params={"since": since})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "source.update"


def test_filters_endpoint_returns_distinct_actions_and_target_types(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    _seed(session, user.id, count=1, action="user.login", target_type=None)
    client = client_for(user)

    response = client.get("/audit/filters")
    assert response.status_code == 200
    body = response.json()
    assert body["actions"] == ["source.create", "user.login"]
    assert body["target_types"] == ["source"]


def test_metadata_is_returned(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, metadata={"name": "app01"})
    client = client_for(user)

    response = client.get("/audit")
    assert response.json()["items"][0]["metadata"] == {"name": "app01"}


def test_integrity_status_unknown_before_any_check_has_run(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    _seed(session, user.id, count=1)
    client = client_for(user)

    response = client.get("/audit/integrity")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "unknown",
        "last_checked_at": None,
        "broken_row_id": None,
        "race_break_count": 0,
        "known_break_count": 0,
        "known_breaks_recorded_at": None,
    }


def test_plain_user_cannot_read_integrity_status(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.get("/audit/integrity")
    assert response.status_code == 403


def test_manual_verify_reports_ok_for_an_intact_chain(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    _seed(session, user.id, count=3)
    client = client_for(user)

    response = client.post("/audit/integrity/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["broken_row_id"] is None
    assert body["last_checked_at"] is not None

    # And the status persists for a plain GET afterward.
    follow_up = client.get("/audit/integrity")
    assert follow_up.json()["status"] == "ok"


def test_manual_verify_detects_a_tampered_row(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    entries = _seed(session, user.id, count=3)
    tampered_id = entries[1].id
    entries[1].action = "tampered"
    session.add(entries[1])
    session.commit()
    client = client_for(user)

    response = client.post("/audit/integrity/verify")
    body = response.json()
    assert body["status"] == "broken"
    assert body["broken_row_id"] == tampered_id


def test_plain_user_cannot_trigger_manual_verify(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.post("/audit/integrity/verify")
    assert response.status_code == 403


def test_manual_verify_is_cooldown_throttled(session, client_for):
    # A safety valve for load: a low-trust view_audit_log-only role could
    # otherwise script rapid repeated calls into real recompute cost on a
    # large log (see the endpoint's own cooldown comment). Tampering
    # *between* two calls made within the cooldown window must still read
    # back the first call's "ok" -- proving the second call actually
    # skipped recomputing, not just that it happened to agree.
    user = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    entries = _seed(session, user.id, count=2)
    client = client_for(user)

    first = client.post("/audit/integrity/verify")
    assert first.json()["status"] == "ok"

    entries[0].action = "tampered"
    session.add(entries[0])
    session.commit()

    second = client.post("/audit/integrity/verify")
    assert second.json()["status"] == "ok"


def _race_forks(session, user_id):
    """Two rows chained from the same tip, as the #213 race wrote them."""
    from app.audit_hash_chain import compute_row_hash
    from app.auth.models import AuditChainState, AuditLog
    from app.crypto import audit_chain_key

    tip, _ = _seed(session, user_id, count=2)
    fork = AuditLog(user_id=None, action="source.open")
    fork.prev_hash = tip.row_hash
    fork.row_hash = compute_row_hash(
        audit_chain_key(),
        tip.row_hash,
        user_id=None,
        action="source.open",
        target_type=None,
        target_id=None,
        timestamp=fork.timestamp,
        metadata=None,
    )
    session.add(fork)
    session.commit()
    session.add(AuditChainState(known_break_cutoff_row_id=fork.id))
    session.commit()
    return fork


def test_super_admin_records_race_breaks_as_known(session, client_for):
    admin = _make_user(session, is_super_admin=True)
    fork = _race_forks(session, admin.id)
    client = client_for(admin)

    verified = client.post("/audit/integrity/verify").json()
    assert (verified["status"], verified["race_break_count"]) == ("race_breaks", 1)
    assert verified["broken_row_id"] == fork.id

    response = client.post("/audit/integrity/known-breaks")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["known_break_count"] == 1
    assert body["known_breaks_recorded_at"] is not None


def test_recording_known_breaks_is_for_super_admins_only(session, client_for):
    viewer = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    _race_forks(session, viewer.id)

    response = client_for(viewer).post("/audit/integrity/known-breaks")
    assert response.status_code == 403


def test_recording_known_breaks_with_none_to_record_is_a_conflict(session, client_for):
    admin = _make_user(session, is_super_admin=True)
    _seed(session, admin.id, count=2)

    response = client_for(admin).post("/audit/integrity/known-breaks")
    assert response.status_code == 409


def _audited_source(session, name="app01"):
    from app.models import Protocol, Source

    source = Source(name=name, protocol=Protocol.ssh, host=name, base_path="/var/log")
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def test_entries_show_the_recorded_target_name(session, client_for):
    """#239: the name recorded with the entry, not the one the object has now."""
    viewer = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    source = _audited_source(session)
    record_audit_event(
        session, user_id=viewer.id, action="source.open", target_type="source", target_id=source.id
    )
    source.name = "renamed"
    session.add(source)
    session.commit()

    item = client_for(viewer).get("/audit").json()["items"][0]
    assert (item["target_name"], item["target_name_is_current"]) == ("app01", False)
    assert item["target_exists"] is True


def test_older_entries_fall_back_to_the_current_name(session, client_for, monkeypatch):
    """Entries written before 0.5.3 have no recorded name and can't get one
    (the hash chain covers them), so the page shows the current name."""
    viewer = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    source = _audited_source(session)
    monkeypatch.setattr("app.audit.audit_labels", lambda *args: {})
    record_audit_event(
        session, user_id=viewer.id, action="source.open", target_type="source", target_id=source.id
    )
    session.commit()
    client = client_for(viewer)

    item = client.get("/audit").json()["items"][0]
    assert (item["target_name"], item["target_name_is_current"]) == ("app01", True)

    session.delete(source)
    session.commit()
    item = client.get("/audit").json()["items"][0]
    assert (item["target_name"], item["target_exists"]) == (None, False)


def test_a_page_of_entries_is_named_in_a_few_queries(session, client_for):
    """Review of #239: naming took several queries per entry (the target,
    its customer, each folder up the tree)."""
    from app.models import Customer, Folder, Protocol, Source
    from sqlalchemy import event

    viewer = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    customer = Customer(name="Vodacom")
    session.add(customer)
    session.commit()
    parent = None
    for depth in range(3):
        folder = Folder(name=f"level{depth}", customer_id=customer.id, parent_folder_id=parent)
        session.add(folder)
        session.commit()
        parent = folder.id
    for index in range(30):
        source = Source(
            name=f"app{index}",
            protocol=Protocol.ssh,
            host=f"app{index}",
            base_path="/var/log",
            customer_id=customer.id,
            folder_id=parent,
        )
        session.add(source)
        session.commit()
        record_audit_event(
            session,
            user_id=viewer.id,
            action="source.open",
            target_type="source",
            target_id=source.id,
            metadata={},
        )
    session.commit()
    session.expire_all()  # as in a fresh request: nothing loaded yet

    statements = []
    engine = session.get_bind()
    listener = lambda *args: statements.append(args[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        items = client_for(viewer).get("/audit").json()["items"]
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert {item["target_context"] for item in items} == {"Vodacom / level0 / level1 / level2"}
    assert len(statements) < 15, statements


# --- sorting (#358) -----------------------------------------------------------


def test_sorting_by_action_covers_every_entry_not_just_the_page(session, client_for):
    user = _make_user(session, is_super_admin=True)
    for action in ("source.update", "folder.create", "user.login", "rule.create"):
        _seed(session, user.id, count=1, action=action)
    client = client_for(user)

    first = client.get("/audit", params={"sort": "action", "order": "asc", "limit": 2}).json()
    second = client.get(
        "/audit", params={"sort": "action", "order": "asc", "limit": 2, "offset": 2}
    ).json()
    actions = [item["action"] for item in first["items"] + second["items"]]
    assert actions == ["folder.create", "rule.create", "source.update", "user.login"]
    descending = client.get("/audit", params={"sort": "action", "order": "desc"}).json()
    assert [item["action"] for item in descending["items"]] == list(reversed(actions))


def test_sorting_by_user_and_the_default_newest_first(session, client_for):
    zed = _make_user(session, is_super_admin=True)
    zed.username = "zed"
    amy = _make_user(session, is_super_admin=True)
    amy.username = "amy"
    session.add_all([zed, amy])
    session.commit()
    _seed(session, zed.id, count=1, action="source.create")
    _seed(session, amy.id, count=1, action="source.update")
    client = client_for(zed)

    by_user = client.get("/audit", params={"sort": "user", "order": "asc"}).json()["items"]
    assert [item["action"] for item in by_user] == ["source.update", "source.create"]
    newest = client.get("/audit").json()["items"]
    assert newest[0]["timestamp"] >= newest[-1]["timestamp"]


def test_an_unknown_sort_is_refused(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)
    assert client.get("/audit", params={"sort": "details"}).status_code == 422
    assert client.get("/audit", params={"order": "sideways"}).status_code == 422


# --- filtering by single targets (#358) ---------------------------------------


def test_filtering_by_single_targets_and_whole_types(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=3, action="source.update", target_type="source")
    _seed(session, user.id, count=2, action="role.update", target_type="role")
    client = client_for(user)

    one = client.get("/audit", params={"target": "source:1"}).json()
    assert [(i["target_type"], i["target_id"]) for i in one["items"]] == [("source", 1)]
    mixed = client.get("/audit", params={"target": ["source:2"], "target_type": ["role"]}).json()
    assert sorted((i["target_type"], i["target_id"]) for i in mixed["items"]) == [
        ("role", 0),
        ("role", 1),
        ("source", 2),
    ]
    assert client.get("/audit", params={"target": "source:x"}).status_code == 422


def test_filter_options_list_each_types_targets_with_a_name(session, client_for):
    user = _make_user(session, is_super_admin=True)
    record_audit_event(
        session,
        user_id=user.id,
        action="source.delete",
        target_type="source",
        target_id=999,
        metadata={"target_name": "gone-source"},
    )
    session.commit()
    _seed(session, user.id, count=1, action="role.update", target_type="role")
    body = client_for(user).get("/audit/filters").json()
    assert {"id": 999, "name": "gone-source"} in body["targets"]["source"]
    assert [t["id"] for t in body["targets"]["role"]] == [0]
