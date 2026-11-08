import uuid

import pytest
from app.api.alerts import router as alerts_router
from app.api.auth import get_current_active_user
from app.auth.models import Capability, Role, ScopeType, User
from app.auth.rbac import create_role_grant
from app.db import get_session
from app.models import Alert, AlertFileState, Customer, Protocol, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


def _make_user(session, *, is_super_admin=False, manage_alerts=True) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=is_super_admin,
        global_capabilities=["manage_alerts"] if manage_alerts else [],
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"user-{uuid.uuid4().hex}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _make_source(session, *, name="app01") -> Source:
    source = Source(
        name=name, protocol=Protocol.ssh, host="app01.example.com", base_path="/var/log"
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(alerts_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def test_create_alert_without_source_scope(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.post(
        "/alerts",
        json={"name": "my alert", "query": "refused", "webhook_url": "https://example.com/hook"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["source_id"] is None
    assert body["enabled"] is True
    assert body["last_checked_at"] is None


def test_create_alert_rejects_non_http_webhook_url(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.post(
        "/alerts",
        json={"name": "x", "query": "q", "webhook_url": "not-a-url"},
    )

    assert response.status_code == 422


def test_create_alert_rejects_webhook_url_pointing_at_a_private_address(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.post(
        "/alerts",
        json={"name": "x", "query": "q", "webhook_url": "http://127.0.0.1:8000/hook"},
    )

    assert response.status_code == 422


def test_create_alert_requires_visible_source(session, client_for):
    user = _make_user(session, is_super_admin=False)
    source = _make_source(session)
    client = client_for(user)

    response = client.post(
        "/alerts",
        json={
            "name": "x",
            "query": "q",
            "source_id": source.id,
            "webhook_url": "https://example.com/hook",
        },
    )

    assert response.status_code == 403


def test_create_alert_succeeds_once_source_is_visible(session, client_for):
    user = _make_user(session, is_super_admin=False)
    customer = Customer(name="Acme")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = _make_source(session)
    source.customer_id = customer.id
    session.add(source)
    session.commit()
    create_role_grant(
        session,
        actor_user_id=None,
        role_id=user.role_id,
        scope_type=ScopeType.customer,
        scope_id=customer.id,
        capabilities=[Capability.view],
    )
    client = client_for(user)

    response = client.post(
        "/alerts",
        json={
            "name": "x",
            "query": "q",
            "source_id": source.id,
            "webhook_url": "https://example.com/hook",
        },
    )

    assert response.status_code == 201
    assert response.json()["source_id"] == source.id


def test_list_alerts_returns_only_own_alerts(session, client_for):
    user_a = _make_user(session)
    user_b = _make_user(session)
    client_a = client_for(user_a)
    client_b = client_for(user_b)

    client_a.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    )
    client_b.post(
        "/alerts", json={"name": "b", "query": "q", "webhook_url": "https://example.com/hook"}
    )

    response = client_a.get("/alerts")

    assert response.status_code == 200
    names = [a["name"] for a in response.json()]
    assert names == ["a"]


def test_update_alert_requires_ownership(session, client_for):
    owner = _make_user(session)
    other = _make_user(session)
    owner_client = client_for(owner)
    other_client = client_for(other)

    created = owner_client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    ).json()

    response = other_client.patch(f"/alerts/{created['id']}", json={"name": "hijacked"})

    assert response.status_code == 404


def test_update_alert_can_disable_and_rename(session, client_for):
    user = _make_user(session)
    client = client_for(user)
    created = client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    ).json()

    response = client.patch(f"/alerts/{created['id']}", json={"enabled": False, "name": "renamed"})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["name"] == "renamed"


def test_update_alert_rejects_webhook_url_pointing_at_a_private_address(session, client_for):
    user = _make_user(session)
    client = client_for(user)
    created = client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    ).json()

    response = client.patch(
        f"/alerts/{created['id']}", json={"webhook_url": "http://169.254.169.254/hook"}
    )

    assert response.status_code == 422


def test_update_alert_rejects_source_the_user_cannot_view(session, client_for):
    user = _make_user(session, is_super_admin=False)
    other_source = _make_source(session, name="not-visible")
    client = client_for(user)
    created = client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    ).json()

    response = client.patch(f"/alerts/{created['id']}", json={"source_id": other_source.id})

    assert response.status_code == 403


def test_delete_alert_requires_ownership(session, client_for):
    owner = _make_user(session)
    other = _make_user(session)
    owner_client = client_for(owner)
    other_client = client_for(other)
    created = owner_client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    ).json()

    assert other_client.delete(f"/alerts/{created['id']}").status_code == 404
    assert owner_client.delete(f"/alerts/{created['id']}").status_code == 204
    assert session.get(Alert, created["id"]) is None


def test_delete_alert_drops_its_per_file_progress(session, client_for):
    owner = _make_user(session)
    source = _make_source(session)
    client = client_for(owner)
    created = client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    ).json()
    session.add(
        AlertFileState(
            alert_id=created["id"],
            source_id=source.id,
            file_path="app.log",
            offset=10,
        )
    )
    session.commit()

    assert client.delete(f"/alerts/{created['id']}").status_code == 204
    assert session.exec(select(AlertFileState)).all() == []


def test_test_endpoint_sends_a_synthetic_webhook(session, client_for, monkeypatch):
    posted = {}
    monkeypatch.setattr(
        "app.api.alerts.post_webhook", lambda url, payload: posted.update(payload) or None
    )

    user = _make_user(session)
    client = client_for(user)
    created = client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    ).json()

    response = client.post(f"/alerts/{created['id']}/test")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "error": None}
    assert len(posted["hits"]) == 1 and posted["test"] is True


def test_creating_an_alert_requires_manage_alerts(session, client_for):
    client = client_for(_make_user(session, manage_alerts=False))
    response = client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    )
    assert response.status_code == 403


def test_without_manage_alerts_you_can_still_list_and_delete_your_alerts(session, client_for):
    user = _make_user(session, manage_alerts=True)
    created = (
        client_for(user)
        .post(
            "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
        )
        .json()
    )
    user.role.global_capabilities = []
    session.add(user.role)
    session.commit()
    client = client_for(user)

    assert client.get("/alerts").status_code == 200
    assert client.patch(f"/alerts/{created['id']}", json={"name": "b"}).status_code == 403
    assert client.post(f"/alerts/{created['id']}/test").status_code == 403
    assert client.delete(f"/alerts/{created['id']}").status_code == 204


def test_alert_changes_are_audited_without_the_full_webhook_url(session, client_for):
    from app.auth.models import AuditLog

    client = client_for(_make_user(session))
    created = client.post(
        "/alerts",
        json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook?token=SECRET"},
    ).json()
    client.patch(f"/alerts/{created['id']}", json={"enabled": False})
    client.delete(f"/alerts/{created['id']}")

    events = session.exec(select(AuditLog).where(AuditLog.target_type == "alert")).all()
    assert [e.action for e in events] == ["alert.create", "alert.update", "alert.delete"]
    assert events[0].event_metadata["webhook_host"] == "example.com"
    assert "SECRET" not in str([e.event_metadata for e in events])


def test_a_folder_scope_counts_sources_in_its_subfolders(session, client_for):
    from app.models import Folder

    customer = Customer(name=f"c-{uuid.uuid4().hex[:6]}")
    session.add(customer)
    session.commit()
    parent = Folder(name="eu", customer_id=customer.id)
    session.add(parent)
    session.commit()
    child = Folder(name="prod", customer_id=customer.id, parent_folder_id=parent.id)
    session.add(child)
    session.commit()
    source = _make_source(session)
    source.customer_id, source.folder_id = customer.id, child.id
    session.add(source)
    session.commit()
    user = _make_user(session)
    create_role_grant(
        session,
        actor_user_id=None,
        role_id=user.role_id,
        scope_type=ScopeType.source,
        scope_id=source.id,
        capabilities=[Capability.view],
    )
    session.commit()

    response = client_for(user).post(
        "/alerts",
        json={
            "name": "a",
            "query": "q",
            "folder_id": parent.id,
            "webhook_url": "https://example.com/hook",
        },
    )
    assert response.status_code == 201, response.text
