import pytest
from app.api.alerts import router as alerts_router
from app.api.auth import get_current_active_user
from app.auth.models import Capability, Role, ScopeType, User
from app.auth.rbac import create_role_grant
from app.db import get_session
from app.models import Alert, Customer, Protocol, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_user(session, *, is_super_admin=False) -> User:
    role = Role(name=f"role-{id(object())}", is_super_admin=is_super_admin)
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"user-{id(object())}@example.com", role_id=role.id)
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


def test_test_endpoint_sends_a_synthetic_webhook(session, client_for, monkeypatch):
    posted = {}
    monkeypatch.setattr(
        "app.api.alerts.send_webhook", lambda alert, hits: posted.update(hits=hits) or True
    )

    user = _make_user(session)
    client = client_for(user)
    created = client.post(
        "/alerts", json={"name": "a", "query": "q", "webhook_url": "https://example.com/hook"}
    ).json()

    response = client.post(f"/alerts/{created['id']}/test")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(posted["hits"]) == 1
