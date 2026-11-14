"""create_source lets a role add sources where it has manage_source;
changing an existing one also needs manage_source on that source (and, to
move it, where it goes)."""

import uuid

import pytest
from app.api.auth import get_current_active_user
from app.api.sources import CREATE_SOURCE_REFUSED, MANAGE_EXISTING_SOURCE_REFUSED
from app.api.sources import router as sources_router
from app.auth.models import Capability, GlobalCapability, Role, RoleGrant, ScopeType, User
from app.collectors import registry
from app.collectors.base import DirEntry
from app.crypto import encrypt_credential
from app.db import get_session
from app.models import Customer, Folder, Protocol, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


def _role(session, *, is_super_admin=False, global_capabilities=None, grants=()) -> Role:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=is_super_admin,
        global_capabilities=global_capabilities or [],
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    for scope_type, scope_id, capabilities in grants:
        session.add(
            RoleGrant(
                role_id=role.id,
                scope_type=scope_type,
                scope_id=scope_id,
                capabilities=capabilities,
            )
        )
    session.commit()
    return role


def _user(session, role: Role) -> User:
    user = User(username=f"user-{uuid.uuid4().hex}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _client(session, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(sources_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _customer(session, name) -> Customer:
    customer = Customer(name=name)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


def _folder(session, customer, name="prod", parent=None) -> Folder:
    folder = Folder(
        name=name, customer_id=customer.id, parent_folder_id=parent.id if parent else None
    )
    session.add(folder)
    session.commit()
    session.refresh(folder)
    return folder


def _source(session, customer, *, folder=None, protocol=Protocol.ssh) -> Source:
    source = Source(
        name=f"src-{uuid.uuid4().hex[:6]}",
        customer_id=customer.id,
        folder_id=folder.id if folder else None,
        protocol=protocol,
        host="h.example.com",
        base_path="/var/log",
        credential_ref=encrypt_credential({"username": "u", "password": "p"}),
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@pytest.fixture()
def two_customers(session):
    return _customer(session, "A"), _customer(session, "B")


@pytest.fixture()
def fake_ssh(monkeypatch):
    class _Module:
        def list_directory(self, source, rules, relative_path=""):
            return [DirEntry(name="log", path="log", is_dir=True, size=0)]

    monkeypatch.setattr(registry.CONNECTORS[Protocol.ssh], "_module", _Module())


def _create_source_with_view_on(session, customer) -> User:
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[(ScopeType.customer, customer.id, [Capability.view])],
    )
    return _user(session, role)


def _assert_refused(response):
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == MANAGE_EXISTING_SOURCE_REFUSED


def test_create_source_alone_cannot_move_a_source_from_another_customer(session, two_customers):
    customer_a, customer_b = two_customers
    source = _source(session, customer_b)
    client = _client(session, _create_source_with_view_on(session, customer_a))

    _assert_refused(client.patch(f"/sources/{source.id}", json={"customer_id": customer_a.id}))
    session.refresh(source)
    assert source.customer_id == customer_b.id


def test_create_source_alone_cannot_change_a_source_it_cannot_manage(
    session, two_customers, fake_ssh
):
    customer_a, customer_b = two_customers
    source = _source(session, customer_b)
    agent = _source(session, customer_b, protocol=Protocol.agent)
    client = _client(session, _create_source_with_view_on(session, customer_a))

    _assert_refused(client.patch(f"/sources/{source.id}", json={"base_path": "/etc"}))
    _assert_refused(client.post(f"/sources/{agent.id}/agent-token"))
    _assert_refused(client.post("/sources/pick-folder", json={"source_id": source.id, "path": "/"}))
    _assert_refused(client.delete(f"/sources/{source.id}"))
    assert session.get(Source, source.id) is not None


def test_view_on_the_source_is_not_enough(session, two_customers):
    customer_a, _ = two_customers
    source = _source(session, customer_a)
    client = _client(session, _create_source_with_view_on(session, customer_a))

    _assert_refused(client.patch(f"/sources/{source.id}", json={"name": "renamed"}))


def test_picker_with_a_typed_credential_needs_no_access_to_the_source(
    session, two_customers, fake_ssh
):
    _, customer_b = two_customers
    source = _source(session, customer_b)
    client = _client(session, _create_source_with_view_on(session, two_customers[0]))

    response = client.post(
        "/sources/pick-folder",
        json={
            "source_id": source.id,
            "credential": {"username": "mine", "password": "typed"},
            "path": "/",
        },
    )
    assert response.status_code == 200, response.text


def test_manage_source_on_source_and_destination_allows_moving(session, two_customers, fake_ssh):
    customer_a, customer_b = two_customers
    source = _source(session, customer_b)
    agent = _source(session, customer_b, protocol=Protocol.agent)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.customer, customer_a.id, [Capability.view, Capability.manage_source]),
            (ScopeType.customer, customer_b.id, [Capability.view, Capability.manage_source]),
        ],
    )
    client = _client(session, _user(session, role))

    assert client.patch(f"/sources/{source.id}", json={"name": "renamed"}).status_code == 200
    assert client.post(f"/sources/{agent.id}/agent-token").status_code == 200
    picked = client.post("/sources/pick-folder", json={"source_id": source.id, "path": "/"})
    assert picked.status_code == 200
    moved = client.patch(f"/sources/{source.id}", json={"customer_id": customer_a.id})
    assert moved.status_code == 200, moved.text
    session.refresh(source)
    assert source.customer_id == customer_a.id
    assert client.delete(f"/sources/{source.id}").status_code == 204


def test_moving_needs_manage_source_at_the_destination(session, two_customers):
    customer_a, customer_b = two_customers
    source = _source(session, customer_b)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.customer, customer_a.id, [Capability.view]),
            (ScopeType.customer, customer_b.id, [Capability.view, Capability.manage_source]),
        ],
    )
    client = _client(session, _user(session, role))

    _assert_refused(client.patch(f"/sources/{source.id}", json={"customer_id": customer_a.id}))
    # Editing it where it is still works.
    assert client.patch(f"/sources/{source.id}", json={"name": "renamed"}).status_code == 200


def test_destination_resolution_walks_the_target_folder_chain(session, two_customers):
    """Moving into a folder is checked as if the source were already there:
    the folder, its ancestors, then the customer -- the most specific grant
    wins, so a narrower grant below a permissive one can still refuse."""
    customer_a, _ = two_customers
    parent = _folder(session, customer_a, "prod")
    child = _folder(session, customer_a, "web", parent=parent)
    locked = _folder(session, customer_a, "locked", parent=parent)
    source = _source(session, customer_a)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.customer, customer_a.id, [Capability.view, Capability.manage_source]),
            (ScopeType.folder, locked.id, [Capability.view]),
        ],
    )
    client = _client(session, _user(session, role))

    _assert_refused(client.patch(f"/sources/{source.id}", json={"folder_id": locked.id}))
    moved = client.patch(
        f"/sources/{source.id}", json={"customer_id": customer_a.id, "folder_id": child.id}
    )
    assert moved.status_code == 200, moved.text


FULL = [Capability.view, Capability.download, Capability.manage_source]


def test_a_move_cannot_widen_the_movers_access(session, two_customers):
    """With no grant of its own, a source inherits from where it lands: a
    destination granting more than where it is now is refused, even with
    manage_source on both."""
    customer_a, _ = two_customers
    target = _folder(session, customer_a, "target")
    source = _source(session, customer_a)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.customer, customer_a.id, [Capability.manage_source]),
            (ScopeType.folder, target.id, FULL),
        ],
    )
    client = _client(session, _user(session, role))

    _assert_refused(client.patch(f"/sources/{source.id}", json={"folder_id": target.id}))
    session.refresh(source)
    assert source.folder_id is None


def test_a_move_to_the_same_or_less_access_is_allowed(session, two_customers):
    customer_a, customer_b = two_customers
    less = _folder(session, customer_a, "less")
    source = _source(session, customer_a)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.customer, customer_a.id, FULL),
            (ScopeType.customer, customer_b.id, FULL),
            (ScopeType.folder, less.id, [Capability.manage_source]),
        ],
    )
    client = _client(session, _user(session, role))

    to_same = client.patch(f"/sources/{source.id}", json={"customer_id": customer_b.id})
    assert to_same.status_code == 200, to_same.text
    to_less = client.patch(
        f"/sources/{source.id}", json={"customer_id": customer_a.id, "folder_id": less.id}
    )
    assert to_less.status_code == 200, to_less.text


def test_a_source_with_its_own_grant_can_move_to_more_access(session, two_customers):
    """Its own grant keeps governing it, so the move changes nothing for the mover."""
    customer_a, _ = two_customers
    target = _folder(session, customer_a, "target")
    source = _source(session, customer_a)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.source, source.id, [Capability.manage_source]),
            (ScopeType.folder, target.id, FULL),
        ],
    )
    client = _client(session, _user(session, role))

    response = client.patch(f"/sources/{source.id}", json={"folder_id": target.id})
    assert response.status_code == 200, response.text


def test_unchanged_location_is_not_a_move(session, two_customers):
    customer_a, _ = two_customers
    source = _source(session, customer_a)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[(ScopeType.source, source.id, [Capability.view, Capability.manage_source])],
    )
    client = _client(session, _user(session, role))

    response = client.patch(
        f"/sources/{source.id}", json={"customer_id": customer_a.id, "name": "same place"}
    )
    assert response.status_code == 200, response.text


def test_super_admin_can_still_do_everything(session, two_customers, fake_ssh):
    customer_a, customer_b = two_customers
    source = _source(session, customer_b)
    agent = _source(session, customer_b, protocol=Protocol.agent)
    client = _client(session, _user(session, _role(session, is_super_admin=True)))

    moved = client.patch(f"/sources/{source.id}", json={"customer_id": customer_a.id})
    assert moved.status_code == 200
    assert client.post(f"/sources/{agent.id}/agent-token").status_code == 200
    picked = client.post("/sources/pick-folder", json={"source_id": source.id, "path": "/"})
    assert picked.status_code == 200
    assert client.delete(f"/sources/{source.id}").status_code == 204


def test_bulk_credentials_skip_sources_it_cannot_manage(session, two_customers):
    """Setting one credential on many sources (#248) follows the same rule,
    source by source: the ones without manage sources are skipped, unnamed and
    unchanged; the others are updated."""
    from app.crypto import decrypt_credential

    customer_a, customer_b = two_customers
    mine = _source(session, customer_a)
    viewed = _source(session, customer_a)
    elsewhere = _source(session, customer_b)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.customer, customer_a.id, [Capability.view]),
            (ScopeType.source, mine.id, [Capability.view, Capability.manage_source]),
        ],
    )
    client = _client(session, _user(session, role))

    response = client.post(
        "/sources/credentials",
        json={
            "source_ids": [mine.id, viewed.id, elsewhere.id],
            "credential": {"username": "svc", "password": "new-pass"},
            "test_first": False,
        },
    )

    assert response.status_code == 200, response.text
    by_id = {r["source_id"]: r for r in response.json()}
    assert by_id[mine.id]["status"] == "updated"
    for refused in (viewed, elsewhere):
        assert by_id[refused.id] == {
            "source_id": refused.id,
            "name": None,
            "status": "skipped",
            "detail": MANAGE_EXISTING_SOURCE_REFUSED,
        }
    session.expire_all()
    assert decrypt_credential(session.get(Source, mine.id).credential_ref)["password"] == "new-pass"
    for refused in (viewed, elsewhere):
        stored = decrypt_credential(session.get(Source, refused.id).credential_ref)
        assert stored["password"] == "p"


def test_bulk_credentials_for_a_super_admin_are_unchanged(session, two_customers):
    customer_a, customer_b = two_customers
    ids = [_source(session, customer_a).id, _source(session, customer_b).id]
    client = _client(session, _user(session, _role(session, is_super_admin=True)))

    response = client.post(
        "/sources/credentials",
        json={
            "source_ids": ids,
            "credential": {"username": "svc", "password": "new-pass"},
            "test_first": False,
        },
    )

    assert [r["status"] for r in response.json()] == ["updated", "updated"]


def _new_source(**scope) -> dict:
    return {
        "name": f"new-{uuid.uuid4().hex[:6]}",
        "protocol": "ssh",
        "host": "h.example.com",
        "base_path": "/var/log",
        **scope,
    }


def test_creating_a_source_needs_manage_source_where_it_goes(session, two_customers):
    customer_a, customer_b = two_customers
    prod = _folder(session, customer_a, "prod")
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.customer, customer_a.id, [Capability.view]),
            (ScopeType.folder, prod.id, [Capability.view, Capability.manage_source]),
        ],
    )
    client = _client(session, _user(session, role))

    for scope in (
        {"customer_id": customer_a.id},
        {"customer_id": customer_b.id},
        {},
    ):
        response = client.post("/sources", json=_new_source(**scope))
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == CREATE_SOURCE_REFUSED
    assert session.exec(select(Source).where(Source.name.startswith("new-"))).first() is None

    created = client.post(
        "/sources", json=_new_source(customer_id=customer_a.id, folder_id=prod.id)
    )
    assert created.status_code == 201, created.text


def test_creating_a_source_with_manage_source_on_the_customer(session, two_customers):
    customer_a, _ = two_customers
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[(ScopeType.customer, customer_a.id, [Capability.manage_source])],
    )
    client = _client(session, _user(session, role))

    response = client.post("/sources", json=_new_source(customer_id=customer_a.id))
    assert response.status_code == 201, response.text


def test_manage_source_without_create_source_cannot_create(session, two_customers):
    customer_a, _ = two_customers
    role = _role(session, grants=[(ScopeType.customer, customer_a.id, [Capability.manage_source])])
    client = _client(session, _user(session, role))

    response = client.post("/sources", json=_new_source(customer_id=customer_a.id))
    assert response.status_code == 403, response.text


def test_super_admin_creates_sources_anywhere(session, two_customers):
    customer_a, _ = two_customers
    client = _client(session, _user(session, _role(session, is_super_admin=True)))

    assert client.post("/sources", json=_new_source()).status_code == 201
    in_a = client.post("/sources", json=_new_source(customer_id=customer_a.id))
    assert in_a.status_code == 201


def test_manage_rules_alone_no_longer_changes_sources(session, two_customers, fake_ssh):
    """manage_rules governs the visibility rules only: with create_source it
    neither edits, moves, deletes nor creates a source any more."""
    customer_a, customer_b = two_customers
    source = _source(session, customer_a)
    agent = _source(session, customer_a, protocol=Protocol.agent)
    rules_only = [Capability.view, Capability.manage_rules]
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[
            (ScopeType.customer, customer_a.id, rules_only),
            (ScopeType.customer, customer_b.id, rules_only),
        ],
    )
    client = _client(session, _user(session, role))

    _assert_refused(client.patch(f"/sources/{source.id}", json={"name": "renamed"}))
    _assert_refused(client.patch(f"/sources/{source.id}", json={"customer_id": customer_b.id}))
    _assert_refused(client.post(f"/sources/{agent.id}/agent-token"))
    _assert_refused(client.post("/sources/pick-folder", json={"source_id": source.id, "path": "/"}))
    _assert_refused(client.delete(f"/sources/{source.id}"))
    created = client.post("/sources", json=_new_source(customer_id=customer_a.id))
    assert created.status_code == 403, created.text
    session.refresh(source)
    assert (source.name, source.customer_id) != ("renamed", customer_b.id)


def test_manage_source_alone_does_not_reach_the_rules(session, two_customers):
    """The two capabilities are separate: managing a source doesn't let a
    role change its rules (reading them only needs view)."""
    from app.api.rules import router as rules_router

    customer_a, _ = two_customers
    source = _source(session, customer_a)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.create_source],
        grants=[(ScopeType.customer, customer_a.id, [Capability.view, Capability.manage_source])],
    )
    user = _user(session, role)
    app = FastAPI()
    app.include_router(rules_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    client = TestClient(app)

    created = client.post(f"/sources/{source.id}/rules", json={"type": "include", "pattern": "**"})
    assert created.status_code == 403, created.text
    assert client.get(f"/sources/{source.id}/rules").json() == []

    # ... while it does change the source itself.
    renamed = _client(session, user).patch(f"/sources/{source.id}", json={"name": "renamed"})
    assert renamed.status_code == 200, renamed.text


def test_my_capabilities_reports_manage_source(session, two_customers):
    customer_a, _ = two_customers
    source = _source(session, customer_a)
    role = _role(
        session,
        grants=[(ScopeType.customer, customer_a.id, [Capability.view, Capability.manage_source])],
    )
    client = _client(session, _user(session, role))

    response = client.get(f"/sources/{source.id}")
    assert response.status_code == 200, response.text
    assert set(response.json()["my_capabilities"]) == {"view", "manage_source"}
