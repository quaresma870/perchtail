from datetime import timedelta

import pytest
from app.agent_registry import get_agent_registry
from app.api.auth import get_current_active_user
from app.api.sources import router as sources_router
from app.auth.models import AuditLog, Capability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Customer, Folder, PatternKind, Protocol, Rule, RuleType, Source
from app.timeutils import utcnow
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_user(session, *, is_super_admin=False, global_capabilities=None) -> User:
    role = Role(
        name=f"role-{is_super_admin}-{global_capabilities}-{id(object())}",
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


def _make_customer(session, name="Vodacom Tanzania") -> Customer:
    customer = Customer(name=name)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(sources_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


@pytest.fixture()
def admin_client(session, client_for):
    return client_for(_make_user(session, is_super_admin=True))


def test_admin_can_create_source_with_credential(session, admin_client):
    customer = _make_customer(session)

    response = admin_client.post(
        "/sources",
        json={
            "name": "app01",
            "customer_id": customer.id,
            "protocol": "ssh",
            "host": "app01.example.com",
            "base_path": "/var/log/appname",
            "credential": {"username": "svc-perchtail", "password": "hunter2"},
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["has_credential"] is True
    assert "credential" not in body
    assert body["rule_count"] == 0


def test_plain_user_cannot_create_source(session, client_for):
    user = _make_user(session)
    client = client_for(user)
    response = client.post(
        "/sources",
        json={"name": "x", "protocol": "local", "host": "localhost", "base_path": "/tmp"},
    )
    assert response.status_code == 403


def test_folder_must_belong_to_given_customer(session, admin_client):
    customer_a = _make_customer(session, "A")
    customer_b = _make_customer(session, "B")
    folder = Folder(name="prod", customer_id=customer_a.id)
    session.add(folder)
    session.commit()
    session.refresh(folder)

    response = admin_client.post(
        "/sources",
        json={
            "name": "x",
            "customer_id": customer_b.id,
            "folder_id": folder.id,
            "protocol": "local",
            "host": "localhost",
            "base_path": "/tmp",
        },
    )
    assert response.status_code == 400


def test_list_sources_filtered_by_rbac(session, client_for):
    customer = _make_customer(session)
    visible = Source(
        name="visible",
        customer_id=customer.id,
        protocol=Protocol.ssh,
        host="h1",
        base_path="/var/log",
    )
    hidden = Source(
        name="hidden",
        customer_id=customer.id,
        protocol=Protocol.ssh,
        host="h2",
        base_path="/var/log",
    )
    session.add(visible)
    session.add(hidden)
    session.commit()
    session.refresh(visible)
    session.refresh(hidden)

    role = Role(name="viewer-role")
    session.add(role)
    session.commit()
    session.refresh(role)
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.source,
            scope_id=visible.id,
            capabilities=[Capability.view],
        )
    )
    session.commit()
    user = User(username="viewer@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)

    client = client_for(user)
    response = client.get("/sources")
    assert response.status_code == 200
    names = {s["name"] for s in response.json()}
    assert names == {"visible"}


def test_system_source_never_listed_for_non_super_admin(session, client_for):
    system_source = Source(
        name="app logs",
        protocol=Protocol.local,
        host="localhost",
        base_path="/var/log/app",
        is_system=True,
    )
    session.add(system_source)
    session.commit()

    role = Role(name="regular")
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username="reg@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)

    response = client_for(user).get("/sources")
    assert response.json() == []


def test_system_source_visible_to_super_admin(session, admin_client):
    system_source = Source(
        name="app logs",
        protocol=Protocol.local,
        host="localhost",
        base_path="/var/log/app",
        is_system=True,
    )
    session.add(system_source)
    session.commit()

    response = admin_client.get("/sources")
    assert any(s["is_system"] for s in response.json())


def test_system_source_cannot_be_updated_or_deleted(session, admin_client):
    system_source = Source(
        name="app logs",
        protocol=Protocol.local,
        host="localhost",
        base_path="/var/log/app",
        is_system=True,
    )
    session.add(system_source)
    session.commit()
    session.refresh(system_source)

    update = admin_client.patch(f"/sources/{system_source.id}", json={"name": "renamed"})
    assert update.status_code == 403

    delete = admin_client.delete(f"/sources/{system_source.id}")
    assert delete.status_code == 403


def test_update_source_and_rotate_credential(session, admin_client):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "app01",
            "customer_id": customer.id,
            "protocol": "ssh",
            "host": "app01",
            "base_path": "/var/log",
        },
    ).json()

    response = admin_client.patch(
        f"/sources/{created['id']}",
        json={"host": "app01-new", "credential": {"username": "u", "password": "p"}},
    )
    assert response.status_code == 200
    assert response.json()["host"] == "app01-new"
    assert response.json()["has_credential"] is True


def test_get_single_source(session, admin_client):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "app01",
            "customer_id": customer.id,
            "protocol": "ssh",
            "host": "app01",
            "base_path": "/var/log",
        },
    ).json()

    response = admin_client.get(f"/sources/{created['id']}")
    assert response.status_code == 200
    assert response.json()["name"] == "app01"


def test_get_single_source_denied_without_grant(session, client_for):
    customer = _make_customer(session)
    source = Source(
        name="app01", customer_id=customer.id, protocol=Protocol.ssh, host="h", base_path="/var/log"
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    user = _make_user(session)
    response = client_for(user).get(f"/sources/{source.id}")
    assert response.status_code == 403


def test_delete_source(session, admin_client):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "app01",
            "customer_id": customer.id,
            "protocol": "ssh",
            "host": "app01",
            "base_path": "/var/log",
        },
    ).json()

    response = admin_client.delete(f"/sources/{created['id']}")
    assert response.status_code == 204
    assert admin_client.get("/sources").json() == []


def test_connection_check_reports_failure_for_unreachable_ssh(session, admin_client):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "app01",
            "customer_id": customer.id,
            "protocol": "ssh",
            "host": "unreachable.invalid",
            "base_path": "/var/log",
            "credential": {"username": "u", "password": "p"},
        },
    ).json()

    response = admin_client.post(f"/sources/{created['id']}/check")
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_connection_check_succeeds_for_local(session, admin_client, tmp_path):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "local-logs",
            "customer_id": customer.id,
            "protocol": "local",
            "host": "localhost",
            "base_path": str(tmp_path),
        },
    ).json()

    response = admin_client.post(f"/sources/{created['id']}/check")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_rule_count_reflects_existing_rules(session, admin_client):
    customer = _make_customer(session)
    source = Source(
        name="app01", customer_id=customer.id, protocol=Protocol.ssh, host="h", base_path="/var/log"
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**/*.log",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()

    response = admin_client.get("/sources")
    assert response.json()[0]["rule_count"] == 1


def test_search_indexing_enabled_defaults_false_and_can_be_toggled(session, admin_client):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "app01",
            "customer_id": customer.id,
            "protocol": "local",
            "host": "localhost",
            "base_path": "/var/log",
        },
    ).json()
    assert created["search_indexing_enabled"] is False

    updated = admin_client.patch(
        f"/sources/{created['id']}", json={"search_indexing_enabled": True}
    ).json()
    assert updated["search_indexing_enabled"] is True


def test_agent_connected_defaults_false_for_non_agent_source(session, admin_client):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "app01",
            "customer_id": customer.id,
            "protocol": "ssh",
            "host": "app01",
            "base_path": "/var/log",
        },
    ).json()
    assert created["agent_connected"] is False
    assert created["agent_last_seen_at"] is None
    assert created["has_agent_token"] is False


def test_regenerate_agent_token_returns_plaintext_once(session, admin_client):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "agent01",
            "customer_id": customer.id,
            "protocol": "agent",
            "host": "agent01",
            "base_path": "/var/log/app",
        },
    ).json()
    assert created["agent_connected"] is False
    assert created["has_agent_token"] is False

    response = admin_client.post(f"/sources/{created['id']}/agent-token")
    assert response.status_code == 200
    token = response.json()["token"]
    assert token

    source = session.get(Source, created["id"])
    assert source.agent_token_hash is not None

    refreshed = admin_client.get(f"/sources/{created['id']}").json()
    assert refreshed["has_agent_token"] is True
    assert source.agent_token_hash != token


def test_regenerate_agent_token_rejects_non_agent_protocol(session, admin_client):
    customer = _make_customer(session)
    created = admin_client.post(
        "/sources",
        json={
            "name": "app01",
            "customer_id": customer.id,
            "protocol": "ssh",
            "host": "app01",
            "base_path": "/var/log",
        },
    ).json()

    response = admin_client.post(f"/sources/{created['id']}/agent-token")
    assert response.status_code == 400


def test_regenerate_agent_token_requires_manage_capability(session, client_for):
    customer = _make_customer(session, "for-plain-user")
    source = Source(
        name="agent01",
        customer_id=customer.id,
        protocol=Protocol.agent,
        host="agent01",
        base_path="/var/log/app",
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    user = _make_user(session)
    response = client_for(user).post(f"/sources/{source.id}/agent-token")
    assert response.status_code == 403


def test_agent_connected_reflects_registry_state(session, admin_client):
    customer = _make_customer(session)
    source = Source(
        name="agent01",
        customer_id=customer.id,
        protocol=Protocol.agent,
        host="agent01",
        base_path="/var/log/app",
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    registry = get_agent_registry()
    registry.register(source.id, object())
    try:
        response = admin_client.get(f"/sources/{source.id}")
        assert response.json()["agent_connected"] is True
    finally:
        registry.unregister(source.id)

    response = admin_client.get(f"/sources/{source.id}")
    assert response.json()["agent_connected"] is False


def test_source_public_includes_customer_and_folder_names(session, admin_client):
    customer = _make_customer(session, "Acme Corp")
    folder = Folder(name="Production", customer_id=customer.id)
    session.add(folder)
    session.commit()
    session.refresh(folder)
    source = Source(
        name="app01",
        customer_id=customer.id,
        folder_id=folder.id,
        protocol=Protocol.ssh,
        host="h1",
        base_path="/var/log",
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    response = admin_client.get(f"/sources/{source.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["customer_name"] == "Acme Corp"
    assert body["folder_name"] == "Production"


def test_source_public_names_are_null_without_customer_or_folder(session, admin_client):
    source = Source(name="orphan", protocol=Protocol.local, host="localhost", base_path="/var/log")
    session.add(source)
    session.commit()
    session.refresh(source)

    response = admin_client.get(f"/sources/{source.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["customer_name"] is None
    assert body["folder_name"] is None


def test_recent_sources_orders_most_recent_first_and_dedupes(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)
    customer = _make_customer(session)
    source_a = Source(
        name="a", customer_id=customer.id, protocol=Protocol.ssh, host="ha", base_path="/var/log"
    )
    source_b = Source(
        name="b", customer_id=customer.id, protocol=Protocol.ssh, host="hb", base_path="/var/log"
    )
    session.add(source_a)
    session.add(source_b)
    session.commit()
    session.refresh(source_a)
    session.refresh(source_b)

    now = utcnow()
    # a opened first, then b, then a again -- each source's *most recent*
    # open should determine its position, and it should appear only once.
    session.add(
        AuditLog(
            user_id=user.id,
            action="source.open",
            target_type="source",
            target_id=source_a.id,
            timestamp=now - timedelta(minutes=10),
        )
    )
    session.add(
        AuditLog(
            user_id=user.id,
            action="source.open",
            target_type="source",
            target_id=source_b.id,
            timestamp=now - timedelta(minutes=5),
        )
    )
    session.add(
        AuditLog(
            user_id=user.id,
            action="source.open",
            target_type="source",
            target_id=source_a.id,
            timestamp=now,
        )
    )
    session.commit()

    response = client.get("/sources/recent")
    assert response.status_code == 200
    names = [s["name"] for s in response.json()]
    assert names == ["a", "b"]


def test_recent_sources_respects_limit(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)
    customer = _make_customer(session)
    sources = []
    for i in range(3):
        s = Source(
            name=f"s{i}",
            customer_id=customer.id,
            protocol=Protocol.ssh,
            host=f"h{i}",
            base_path="/var/log",
        )
        session.add(s)
        sources.append(s)
    session.commit()
    for s in sources:
        session.refresh(s)

    now = utcnow()
    for i, s in enumerate(sources):
        session.add(
            AuditLog(
                user_id=user.id,
                action="source.open",
                target_type="source",
                target_id=s.id,
                timestamp=now - timedelta(minutes=i),
            )
        )
    session.commit()

    response = client.get("/sources/recent", params={"limit": 2})
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_recent_sources_excludes_source_no_longer_visible(session, client_for):
    # A non-super-admin user who opened a source in the past, then had their
    # grant on it revoked -- GET /sources/recent must not leak it back
    # through history even though the AuditLog row still exists.
    customer = _make_customer(session)
    source = Source(
        name="app01",
        customer_id=customer.id,
        protocol=Protocol.ssh,
        host="h1",
        base_path="/var/log",
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    role = Role(name="viewer-role-recent")
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username="viewer-recent@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)

    session.add(
        AuditLog(user_id=user.id, action="source.open", target_type="source", target_id=source.id)
    )
    session.commit()

    client = client_for(user)
    response = client.get("/sources/recent")
    assert response.status_code == 200
    assert response.json() == []
