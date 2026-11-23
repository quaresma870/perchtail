import json
import uuid

import pytest
from app.api.auth import get_current_active_user
from app.api.sso import router as sso_router
from app.auth.models import AuditLog, GlobalCapability, Role, SSOProviderConfig, User
from app.crypto import decrypt_secret
from app.db import get_session
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


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


def test_an_http_issuer_is_refused_on_create_and_update(manage_client, session):
    insecure = {**CREATE_PAYLOAD, "issuer": "http://idp.example.com"}
    response = manage_client.post("/sso", json=insecure)
    assert response.status_code == 422
    assert response.json()["detail"] == "The issuer URL must be an https:// URL"
    assert session.exec(select(SSOProviderConfig)).all() == []

    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    response = manage_client.patch(f"/sso/{created['id']}", json={"issuer": "http://x.example"})
    assert response.status_code == 422
    assert manage_client.get(f"/sso/{created['id']}").json()["issuer"] == CREATE_PAYLOAD["issuer"]


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


def test_delete_removes_the_provider(session, client_for):
    # Deleting the provider is for super admins only (see below).
    client = client_for(_make_user(session, is_super_admin=True))
    created = client.post("/sso", json=CREATE_PAYLOAD).json()
    response = client.delete(f"/sso/{created['id']}")
    assert response.status_code == 204
    assert session.get(SSOProviderConfig, created["id"]) is None


def test_get_missing_provider_is_404(manage_client):
    response = manage_client.get("/sso/999")
    assert response.status_code == 404


def _make_unreadable(session, provider_id: int) -> None:
    """As after losing CREDENTIAL_ENCRYPTION_KEY or the salt: the stored
    settings are encrypted under a key PerchTail no longer has."""
    config = session.get(SSOProviderConfig, provider_id)
    config.config = Fernet(Fernet.generate_key()).encrypt(b'{"issuer": "x"}').decode()
    session.add(config)
    session.commit()


def test_unreadable_provider_is_still_listed_with_its_plain_fields(manage_client, session):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    _make_unreadable(session, created["id"])

    listed = manage_client.get("/sso")
    fetched = manage_client.get(f"/sso/{created['id']}")

    assert listed.status_code == 200
    assert fetched.status_code == 200
    [body] = listed.json()
    assert body["secret_unreadable"] is True
    assert body["name"] == "Corporate SSO"
    assert body["enabled"] is True
    assert body["issuer"] == "" and body["client_id"] == ""
    assert fetched.json()["secret_unreadable"] is True


def test_readable_provider_is_not_flagged(manage_client):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    assert created["secret_unreadable"] is False


def test_unreadable_provider_needs_the_settings_again_before_saving(manage_client, session):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    _make_unreadable(session, created["id"])

    response = manage_client.patch(f"/sso/{created['id']}", json={"name": "Renamed"})

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "The saved client secret can't be decrypted with the current key; enter it again"
    )


def test_unreadable_provider_accepts_a_new_secret(manage_client, session):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    _make_unreadable(session, created["id"])

    response = manage_client.patch(
        f"/sso/{created['id']}",
        json={
            "issuer": "https://idp.example.com",
            "client_id": "perchtail-client",
            "client_secret": "new-secret",
        },
    )

    assert response.status_code == 200
    assert response.json()["secret_unreadable"] is False
    session.expire_all()
    stored = json.loads(decrypt_secret(session.get(SSOProviderConfig, created["id"]).config))
    assert stored["client_secret"] == "new-secret"
    assert stored["scopes"] == "openid email profile"


def test_connection_check_reports_unreadable_settings(manage_client, session):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    _make_unreadable(session, created["id"])

    response = manage_client.post(f"/sso/{created['id']}/test")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "can't be decrypted" in response.json()["detail"]


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
    # The reason goes to the log, not to the caller.
    assert "could not connect" not in response.json()["detail"]
    assert response.json()["detail"] == sso_module.IDP_CHECK_FAILED_DETAIL


@pytest.mark.parametrize(
    "issuer",
    [
        "https://127.0.0.1:8000",
        "https://10.0.0.5",
        "https://169.254.169.254",
        "https://[::1]",
    ],
)
def test_connection_check_never_reaches_an_internal_address(manage_client, monkeypatch, issuer):
    from app.api import sso as sso_module
    from app.auth.providers import oidc
    from structlog.testing import capture_logs

    monkeypatch.setattr(
        oidc.httpx, "Client", lambda *a, **k: pytest.fail("no request to an internal address")
    )
    created = manage_client.post("/sso", json={**CREATE_PAYLOAD, "issuer": issuer}).json()

    with capture_logs() as logs:
        response = manage_client.post(f"/sso/{created['id']}/test")

    assert response.json() == {"ok": False, "detail": sso_module.IDP_CHECK_FAILED_DETAIL}
    [entry] = [e for e in logs if e["event"] == "sso.test.failed"]
    assert entry["error"] == "UnsafeWebhookURLError"


def test_connection_check_never_follows_a_jwks_uri_to_an_internal_address(
    manage_client, monkeypatch
):
    from app.api import sso as sso_module
    from app.auth.providers import oidc

    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()
    monkeypatch.setattr(
        sso_module,
        "fetch_discovery_document",
        lambda issuer: {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://127.0.0.1:8000/healthz",
        },
    )
    monkeypatch.setattr(
        oidc.httpx, "Client", lambda *a, **k: pytest.fail("no request to an internal address")
    )

    response = manage_client.post(f"/sso/{created['id']}/test")

    assert response.json() == {"ok": False, "detail": sso_module.IDP_CHECK_FAILED_DETAIL}


def test_connection_check_reports_incomplete_discovery_document(manage_client, monkeypatch):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()

    from app.api import sso as sso_module

    monkeypatch.setattr(
        sso_module, "fetch_discovery_document", lambda issuer: {"token_endpoint": "https://x"}
    )

    response = manage_client.post(f"/sso/{created['id']}/test")
    assert response.status_code == 200
    assert response.json() == {"ok": False, "detail": sso_module.IDP_CHECK_FAILED_DETAIL}


def test_connection_check_refuses_and_audits_another_issuer(manage_client, session, monkeypatch):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()

    from app.api import sso as sso_module
    from app.auth.providers import oidc

    def _mismatch(issuer):
        raise oidc.IssuerMismatchError(
            "The identity provider's discovery document names a different issuer"
        )

    monkeypatch.setattr(sso_module, "fetch_discovery_document", _mismatch)
    monkeypatch.setattr(sso_module, "fetch_jwks", lambda jwks_uri: pytest.fail("no JWKS request"))

    response = manage_client.post(f"/sso/{created['id']}/test")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "detail": sso_module.IDP_CHECK_FAILED_DETAIL}
    session.expire_all()
    [entry] = session.exec(
        select(AuditLog).where(AuditLog.action == "sso_provider.issuer_mismatch")
    ).all()
    assert (entry.target_type, entry.target_id) == ("sso_provider", created["id"])


# --- group_claim on the provider config ---------------------------------------------


def test_create_provider_with_group_claim(manage_client):
    payload = {**CREATE_PAYLOAD, "group_claim": "groups"}
    response = manage_client.post("/sso", json=payload)
    assert response.status_code == 201
    assert response.json()["group_claim"] == "groups"


def test_create_provider_defaults_group_claim_to_null(manage_client):
    response = manage_client.post("/sso", json=CREATE_PAYLOAD)
    assert response.json()["group_claim"] is None


def test_update_can_set_and_clear_group_claim(manage_client):
    created = manage_client.post("/sso", json=CREATE_PAYLOAD).json()

    set_response = manage_client.patch(f"/sso/{created['id']}", json={"group_claim": "groups"})
    assert set_response.json()["group_claim"] == "groups"

    cleared_response = manage_client.patch(f"/sso/{created['id']}", json={"group_claim": ""})
    assert cleared_response.json()["group_claim"] is None


# --- group-role mappings --------------------------------------------------------------


def _make_role(session, name="Tier2") -> int:
    from app.auth.rbac import create_role

    role = create_role(session, actor_user_id=None, name=name)
    return role.id


def test_list_group_mappings_requires_manage_sso(plain_client):
    assert plain_client.get("/sso/group-mappings").status_code == 403


def test_create_and_list_group_mapping(manage_client, session):
    role_id = _make_role(session)

    created = manage_client.post(
        "/sso/group-mappings", json={"order": 0, "group_name": "engineering", "role_id": role_id}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["group_name"] == "engineering"
    assert body["role_id"] == role_id
    assert body["role_name"] == "Tier2"

    listed = manage_client.get("/sso/group-mappings")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_list_group_mappings_is_ordered(manage_client, session):
    role_id = _make_role(session)
    manage_client.post(
        "/sso/group-mappings", json={"order": 5, "group_name": "b", "role_id": role_id}
    )
    manage_client.post(
        "/sso/group-mappings", json={"order": 1, "group_name": "a", "role_id": role_id}
    )

    listed = manage_client.get("/sso/group-mappings").json()
    assert [m["group_name"] for m in listed] == ["a", "b"]


def test_create_group_mapping_rejects_unknown_role(manage_client):
    response = manage_client.post(
        "/sso/group-mappings", json={"order": 0, "group_name": "engineering", "role_id": 99999}
    )
    assert response.status_code == 404


def test_update_group_mapping(manage_client, session):
    role_id = _make_role(session)
    other_role_id = _make_role(session, name="Tier3")
    created = manage_client.post(
        "/sso/group-mappings", json={"order": 0, "group_name": "engineering", "role_id": role_id}
    ).json()

    response = manage_client.patch(
        f"/sso/group-mappings/{created['id']}",
        json={"group_name": "platform", "role_id": other_role_id},
    )
    assert response.status_code == 200
    assert response.json()["group_name"] == "platform"
    assert response.json()["role_name"] == "Tier3"


def test_delete_group_mapping(manage_client, session):
    role_id = _make_role(session)
    created = manage_client.post(
        "/sso/group-mappings", json={"order": 0, "group_name": "engineering", "role_id": role_id}
    ).json()

    assert manage_client.delete(f"/sso/group-mappings/{created['id']}").status_code == 204
    assert manage_client.get("/sso/group-mappings").json() == []


def test_get_missing_group_mapping_is_404(manage_client):
    assert manage_client.patch("/sso/group-mappings/99999", json={"order": 1}).status_code == 404
    assert manage_client.delete("/sso/group-mappings/99999").status_code == 404


# --- delegated SSO admins can't repoint trust past their own authority ----------


def _super_admin_mapping(session) -> None:
    from app.auth.models import SSOGroupRoleMapping

    role = Role(name=f"Super-{uuid.uuid4().hex}", is_super_admin=True)
    session.add(role)
    session.commit()
    session.add(SSOGroupRoleMapping(order=1, group_name="admins", role_id=role.id))
    session.commit()


def _seed_provider(session, *, enabled=True) -> int:
    from app.crypto import encrypt_secret

    config = SSOProviderConfig(
        protocol="oidc",
        name="Corporate SSO",
        config=encrypt_secret(
            json.dumps(
                {
                    "issuer": "https://idp.example.com",
                    "client_id": "perchtail-client",
                    "client_secret": "s3cret-value",
                    "scopes": "openid email profile",
                    "group_claim": "groups",
                }
            )
        ),
        enabled=enabled,
    )
    session.add(config)
    session.commit()
    session.refresh(config)
    return config.id


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://attacker-idp.example.com"},
        {"client_id": "attacker-client"},
        {"group_claim": "nickname"},
        {"enabled": True},
    ],
)
def test_delegated_sso_admin_cannot_change_trust_while_a_mapping_exceeds_them(
    manage_client, session, change
):
    _super_admin_mapping(session)
    provider_id = _seed_provider(session, enabled=False)

    response = manage_client.patch(f"/sso/{provider_id}", json=change)

    assert response.status_code == 403
    raw = json.loads(decrypt_secret(session.get(SSOProviderConfig, provider_id).config))
    assert raw["issuer"] == "https://idp.example.com"
    assert raw["client_id"] == "perchtail-client"
    assert raw["group_claim"] == "groups"


def test_delegated_sso_admin_cannot_create_a_provider_while_a_mapping_exceeds_them(
    manage_client, session
):
    _super_admin_mapping(session)

    response = manage_client.post("/sso", json=CREATE_PAYLOAD)

    assert response.status_code == 403
    assert session.exec(select(SSOProviderConfig)).all() == []


def test_delegated_sso_admin_can_still_make_non_trust_changes(manage_client, session):
    _super_admin_mapping(session)
    provider_id = _seed_provider(session)

    response = manage_client.patch(
        f"/sso/{provider_id}",
        json={"name": "Renamed", "client_secret": "rotated", "issuer": "https://idp.example.com"},
    )

    assert response.status_code == 200, response.text


# --- removing trust and mappings: super admins, and authority -------------------------


def test_a_delegated_sso_admin_cannot_delete_the_provider(manage_client, session):
    provider_id = _seed_provider(session)

    response = manage_client.delete(f"/sso/{provider_id}")

    assert response.status_code == 403
    assert session.get(SSOProviderConfig, provider_id) is not None


def test_a_delegated_sso_admin_cannot_turn_the_provider_off(manage_client, session):
    provider_id = _seed_provider(session, enabled=True)

    response = manage_client.patch(f"/sso/{provider_id}", json={"enabled": False})

    assert response.status_code == 403
    session.expire_all()
    assert session.get(SSOProviderConfig, provider_id).enabled is True


def test_a_super_admin_can_turn_off_and_delete_the_provider(session, client_for):
    provider_id = _seed_provider(session, enabled=True)
    client = client_for(_make_user(session, is_super_admin=True))

    assert client.patch(f"/sso/{provider_id}", json={"enabled": False}).status_code == 200
    assert client.delete(f"/sso/{provider_id}").status_code == 204


def test_a_delegated_sso_admin_can_still_leave_a_disabled_provider_disabled(manage_client, session):
    provider_id = _seed_provider(session, enabled=False)

    response = manage_client.patch(f"/sso/{provider_id}", json={"enabled": False, "name": "X"})

    assert response.status_code == 200


def _mapping_to(session, role: Role, group="admins") -> int:
    from app.auth.models import SSOGroupRoleMapping

    mapping = SSOGroupRoleMapping(order=1, group_name=group, role_id=role.id)
    session.add(mapping)
    session.commit()
    session.refresh(mapping)
    return mapping.id


def _super_role(session) -> Role:
    role = Role(name=f"Super-{uuid.uuid4().hex}", is_super_admin=True)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def test_a_delegated_sso_admin_cannot_delete_a_super_admin_mapping(manage_client, session):
    from app.auth.models import SSOGroupRoleMapping

    mapping_id = _mapping_to(session, _super_role(session))

    response = manage_client.delete(f"/sso/group-mappings/{mapping_id}")

    assert response.status_code == 403
    assert session.get(SSOGroupRoleMapping, mapping_id) is not None


def test_a_delegated_sso_admin_cannot_delete_a_mapping_beyond_their_authority(
    manage_client, session
):
    from app.auth.models import SSOGroupRoleMapping

    wider = Role(name="Auditors", global_capabilities=[GlobalCapability.view_audit_log])
    session.add(wider)
    session.commit()
    mapping_id = _mapping_to(session, wider, group="auditors")

    response = manage_client.delete(f"/sso/group-mappings/{mapping_id}")

    assert response.status_code == 403
    assert session.get(SSOGroupRoleMapping, mapping_id) is not None


def test_a_delegated_sso_admin_cannot_repoint_or_reorder_a_super_admin_mapping(
    manage_client, session
):
    mapping_id = _mapping_to(session, _super_role(session))
    viewer_id = _make_role(session, name="Viewer")

    for change in ({"role_id": viewer_id}, {"order": 99}, {"group_name": "everyone"}):
        response = manage_client.patch(f"/sso/group-mappings/{mapping_id}", json=change)
        assert response.status_code == 403


def test_a_delegated_sso_admin_cannot_map_a_group_to_a_super_admin_role(manage_client, session):
    role = _super_role(session)

    response = manage_client.post(
        "/sso/group-mappings", json={"order": 0, "group_name": "x", "role_id": role.id}
    )

    assert response.status_code == 403


def _mapping_at(session, role_id: int, *, group: str, order: int) -> int:
    from app.auth.models import SSOGroupRoleMapping

    mapping = SSOGroupRoleMapping(order=order, group_name=group, role_id=role_id)
    session.add(mapping)
    session.commit()
    session.refresh(mapping)
    return mapping.id


def test_a_delegated_sso_admin_cannot_override_a_super_admin_mapping_with_a_later_one(session):
    # Last match wins: a catch-all group mapped to a lesser role after the
    # super-admin mapping would take Super Admin away from everyone in both.
    from app.auth.models import SSOGroupRoleMapping
    from app.auth.sessions import create_session
    from app.main import app

    _mapping_at(session, _super_role(session).id, group="admins", order=1)
    viewer_id = _make_role(session, name="Viewer")
    admin = _make_user(session, global_capabilities=[GlobalCapability.manage_sso])
    _, token = create_session(session, admin)
    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        client.cookies.set("perchtail_session", token)
        response = client.post(
            "/sso/group-mappings",
            json={"order": 999, "group_name": "everyone", "role_id": viewer_id},
        )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 403
    session.expire_all()
    assert (
        session.exec(
            select(SSOGroupRoleMapping).where(SSOGroupRoleMapping.group_name == "everyone")
        ).all()
        == []
    )
    [denied] = session.exec(select(AuditLog).where(AuditLog.action == "access.denied")).all()
    assert denied.user_id == admin.id
    assert denied.event_metadata["reason"] == "mapping_order_beyond_authority"


def test_a_delegated_sso_admin_cannot_uncover_a_super_admin_mapping_by_deleting_a_later_one(
    manage_client, session
):
    # Deleting the lesser mapping that overrides the super-admin one would
    # hand Super Admin to everyone in both groups.
    from app.auth.models import SSOGroupRoleMapping

    _mapping_at(session, _super_role(session).id, group="admins", order=1)
    contractors = _mapping_at(
        session, _make_role(session, name="Viewer"), group="contractors", order=2
    )

    response = manage_client.delete(f"/sso/group-mappings/{contractors}")

    assert response.status_code == 403
    assert session.get(SSOGroupRoleMapping, contractors) is not None


def test_a_delegated_sso_admin_cannot_move_a_mapping_across_a_super_admin_one(
    manage_client, session
):
    _mapping_at(session, _super_role(session).id, group="admins", order=5)
    viewer_id = _make_role(session, name="Viewer")
    before = _mapping_at(session, viewer_id, group="everyone", order=1)
    after = _mapping_at(session, viewer_id, group="contractors", order=9)

    # Moving past it (overriding) and moving out from behind it (uncovering),
    # and changing the group of one that already overrides it.
    for mapping_id, change in (
        (before, {"order": 9}),
        (after, {"order": 0}),
        (after, {"group_name": "staff"}),
        (after, {"role_id": _make_role(session, name="Other")}),
    ):
        response = manage_client.patch(f"/sso/group-mappings/{mapping_id}", json=change)
        assert response.status_code == 403, change


def test_a_delegated_sso_admin_can_work_on_mappings_before_a_super_admin_one(
    manage_client, session
):
    _mapping_at(session, _super_role(session).id, group="admins", order=5)
    viewer_id = _make_role(session, name="Viewer")
    existing = _mapping_at(session, viewer_id, group="everyone", order=1)

    created = manage_client.post(
        "/sso/group-mappings", json={"order": 2, "group_name": "staff", "role_id": viewer_id}
    )
    assert created.status_code == 201
    moved = manage_client.patch(f"/sso/group-mappings/{existing}", json={"order": 3})
    assert moved.status_code == 200
    assert manage_client.delete(f"/sso/group-mappings/{existing}").status_code == 204


def test_a_super_admin_can_order_mappings_around_a_super_admin_one(session, client_for):
    _mapping_at(session, _super_role(session).id, group="admins", order=1)
    viewer_id = _make_role(session, name="Viewer")
    client = client_for(_make_user(session, is_super_admin=True))

    created = client.post(
        "/sso/group-mappings", json={"order": 9, "group_name": "everyone", "role_id": viewer_id}
    )
    assert created.status_code == 201
    mapping_id = created.json()["id"]
    assert client.patch(f"/sso/group-mappings/{mapping_id}", json={"order": 0}).status_code == 200
    assert client.delete(f"/sso/group-mappings/{mapping_id}").status_code == 204


def test_a_super_admin_can_delete_a_super_admin_mapping(session, client_for):
    mapping_id = _mapping_to(session, _super_role(session))
    client = client_for(_make_user(session, is_super_admin=True))

    assert client.delete(f"/sso/group-mappings/{mapping_id}").status_code == 204


def test_saving_the_provider_empties_the_discovery_cache(session, client_for, monkeypatch):
    client = client_for(_make_user(session, is_super_admin=True))
    cleared = []
    monkeypatch.setattr("app.api.sso.clear_discovery_cache", lambda: cleared.append(True))

    created = client.post("/sso", json={**CREATE_PAYLOAD, "enabled": False}).json()
    client.patch(f"/sso/{created['id']}", json={"name": "Renamed"})
    client.delete(f"/sso/{created['id']}")

    assert len(cleared) == 3


def test_super_admin_can_change_trust_with_any_mapping(session, client_for):
    _super_admin_mapping(session)
    provider_id = _seed_provider(session)
    client = client_for(_make_user(session, is_super_admin=True))

    response = client.patch(f"/sso/{provider_id}", json={"issuer": "https://new-idp.example.com"})

    assert response.status_code == 200


def test_delegated_sso_admin_can_change_trust_when_mappings_are_within_authority(
    manage_client, session
):
    from app.auth.models import SSOGroupRoleMapping

    role = Role(name="Viewer")
    session.add(role)
    session.commit()
    session.add(SSOGroupRoleMapping(order=1, group_name="viewers", role_id=role.id))
    session.commit()
    provider_id = _seed_provider(session)

    response = manage_client.patch(
        f"/sso/{provider_id}", json={"issuer": "https://new-idp.example.com"}
    )

    assert response.status_code == 200
