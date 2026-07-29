import json

import pytest
from app.api.auth import get_current_active_user
from app.api.sso import router as sso_router
from app.auth.models import GlobalCapability, Role, SSOProviderConfig, User
from app.crypto import decrypt_secret
from app.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


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


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(sso_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


@pytest.fixture()
def manage_client(session, client_for):
    return client_for(_make_user(session, global_capabilities=[GlobalCapability.manage_sso]))


@pytest.fixture()
def plain_client(session, client_for):
    return client_for(_make_user(session))


CREATE_PAYLOAD = {
    "name": "Corporate SSO",
    "issuer": "https://idp.example.com",
    "client_id": "perchtail-client",
    "client_secret": "s3cret-value",
    "scopes": "openid email profile",
    "enabled": True,
}


def test_create_requires_manage_sso(plain_client):
    response = plain_client.post("/sso", json=CREATE_PAYLOAD)
    assert response.status_code == 403


def test_create_and_get_provider(manage_client):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD)
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Corporate SSO"
    assert body["issuer"] == "https://idp.example.com"
    assert body["client_id"] == "perchtail-client"
    assert "client_secret" not in body

    fetched = manage_client.get(f"/sso/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Corporate SSO"


def test_client_secret_is_encrypted_at_rest(manage_client, session):
    manage_client.post("/sso", json=CREATE_PAYLOAD)
    config = session.exec(select(SSOProviderConfig)).first()
    assert "s3cret-value" not in config.config


def test_list_returns_all_providers_without_secrets(manage_client):
    manage_client.post("/sso", json=CREATE_PAYLOAD)
    response = manage_client.get("/sso")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert "client_secret" not in response.json()[0]


def test_second_enabled_provider_is_rejected(manage_client):
    manage_client.post("/sso", json=CREATE_PAYLOAD)
    second = {**CREATE_PAYLOAD, "name": "Another IdP", "issuer": "https://idp2.example.com"}
    response = manage_client.post("/sso", json=second)
    assert response.status_code == 409


def test_a_second_disabled_provider_is_allowed(manage_client):
    manage_client.post("/sso", json=CREATE_PAYLOAD)
    second = {**CREATE_PAYLOAD, "name": "Another IdP", "enabled": False}
    response = manage_client.post("/sso", json=second)
    assert response.status_code == 201


def test_update_can_change_fields_without_resetting_the_secret(manage_client, session):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()

    response = manage_client.patch(f"/sso/{created['id']}", json={"name": "Renamed IdP"})
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed IdP"
    assert response.json()["client_id"] == "perchtail-client"

    # Fernet re-encrypts non-deterministically (fresh IV per call), so the
    # raw ciphertext always differs after any update — decrypt and compare
    # the actual secret value instead of the blob.
    stored = json.loads(decrypt_secret(session.get(SSOProviderConfig, created["id"]).config))
    assert stored["client_secret"] == "s3cret-value"


def test_update_enabling_a_second_provider_is_rejected(manage_client):
    first = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    second = manage_client.post(
        "/sso", json={**CREATE_PAYLOAD, "name": "Another IdP", "enabled": False}
    ).json()

    response = manage_client.patch(f"/sso/{second['id']}", json={"enabled": True})
    assert response.status_code == 409

    # Re-enabling the same already-enabled provider is fine (not "another").
    response = manage_client.patch(f"/sso/{first['id']}", json={"enabled": True})
    assert response.status_code == 200


def test_delete_removes_the_provider(manage_client, session):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    response = manage_client.delete(f"/sso/{created['id']}")
    assert response.status_code == 204
    assert session.get(SSOProviderConfig, created["id"]) is None


def test_get_missing_provider_is_404(manage_client):
    response = manage_client.get("/sso/999")
    assert response.status_code == 404


def test_connection_check_success(manage_client, monkeypatch):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()

    from app.api import sso as sso_module

    monkeypatch.setattr(
        sso_module,
        "fetch_discovery_document",
        lambda issuer: {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/jwks",
        },
    )
    monkeypatch.setattr(sso_module, "fetch_jwks", lambda jwks_uri: {"keys": []})

    response = manage_client.post(f"/sso/{created['id']}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_connection_check_reports_unreachable_idp(manage_client, monkeypatch):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()

    from app.api import sso as sso_module

    def _boom(issuer):
        raise ConnectionError("could not connect")

    monkeypatch.setattr(sso_module, "fetch_discovery_document", _boom)

    response = manage_client.post(f"/sso/{created['id']}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "could not connect" in response.json()["detail"]


def test_connection_check_reports_incomplete_discovery_document(manage_client, monkeypatch):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()

    from app.api import sso as sso_module

    monkeypatch.setattr(
        sso_module, "fetch_discovery_document", lambda issuer: {"token_endpoint": "https://x"}
    )

    response = manage_client.post(f"/sso/{created['id']}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "authorization_endpoint" in response.json()["detail"]
