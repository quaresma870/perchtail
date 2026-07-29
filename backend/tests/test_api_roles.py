import pytest
from app.api.auth import get_current_active_user
from app.api.roles import router as roles_router
from app.auth.models import GlobalCapability, Role, User
from app.db import get_session
from app.models import Customer, Protocol, Source
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


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(roles_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


@pytest.fixture()
def admin_client(session, client_for):
    return client_for(_make_user(session, is_super_admin=True))


@pytest.fixture()
def manage_roles_client(session, client_for):
    return client_for(_make_user(session, global_capabilities=[GlobalCapability.manage_roles]))


def test_manage_roles_capability_can_create_role(session, manage_roles_client):
    response = manage_roles_client.post(
        "/roles", json={"name": "Tier 2 — Vodacom", "global_capabilities": []}
    )
    assert response.status_code == 201
    assert response.json()["is_super_admin"] is False


def test_plain_user_cannot_manage_roles(session, client_for):
    user = _make_user(session)
    client = client_for(user)
    assert client.get("/roles").status_code == 403


def test_non_super_admin_cannot_create_super_admin_role(session, manage_roles_client):
    response = manage_roles_client.post(
        "/roles", json={"name": "Sneaky Admin", "is_super_admin": True}
    )
    assert response.status_code == 403


def test_super_admin_can_create_super_admin_role(session, admin_client):
    response = admin_client.post("/roles", json={"name": "Break Glass", "is_super_admin": True})
    assert response.status_code == 201


def test_duplicate_role_name_rejected(session, admin_client):
    admin_client.post("/roles", json={"name": "Support"})
    response = admin_client.post("/roles", json={"name": "Support"})
    assert response.status_code == 409


def test_cannot_delete_builtin_role(session, admin_client):
    from app.auth.rbac import create_role

    role = create_role(session, actor_user_id=None, name="Builtin", is_builtin=True)
    response = admin_client.delete(f"/roles/{role.id}")
    assert response.status_code == 403


def test_cannot_delete_role_with_users(session, admin_client):
    role = admin_client.post("/roles", json={"name": "Tier 1"}).json()
    session.add(User(username="assigned@example.com", role_id=role["id"]))
    session.commit()

    response = admin_client.delete(f"/roles/{role['id']}")
    assert response.status_code == 409


def test_delete_role_without_users(session, admin_client):
    role = admin_client.post("/roles", json={"name": "Unused"}).json()
    response = admin_client.delete(f"/roles/{role['id']}")
    assert response.status_code == 204


def test_duplicate_role_clones_name_and_grants(session, admin_client):
    customer = Customer(name="Vodacom Tanzania")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    role = admin_client.post("/roles", json={"name": "Tier 2"}).json()
    admin_client.post(
        f"/roles/{role['id']}/grants",
        json={"scope_type": "customer", "scope_id": customer.id, "capabilities": ["view"]},
    )

    duplicated = admin_client.post(f"/roles/{role['id']}/duplicate", json={})
    assert duplicated.status_code == 201
    assert duplicated.json()["name"] == "Tier 2 (copy)"

    grants = admin_client.get(f"/roles/{duplicated.json()['id']}/grants").json()
    assert len(grants) == 1
    assert grants[0]["scope_id"] == customer.id


def test_duplicate_role_name_conflict(session, admin_client):
    role = admin_client.post("/roles", json={"name": "Tier 2"}).json()
    admin_client.post("/roles", json={"name": "Tier 2 (copy)"})

    response = admin_client.post(f"/roles/{role['id']}/duplicate", json={})
    assert response.status_code == 409


def test_grant_crud_validates_scope_existence(session, admin_client):
    role = admin_client.post("/roles", json={"name": "Tier 2"}).json()

    response = admin_client.post(
        f"/roles/{role['id']}/grants",
        json={"scope_type": "customer", "scope_id": 999999, "capabilities": ["view"]},
    )
    assert response.status_code == 404


def test_grant_update_and_delete(session, admin_client):
    customer = Customer(name="Vodacom Tanzania")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    role = admin_client.post("/roles", json={"name": "Tier 2"}).json()

    grant = admin_client.post(
        f"/roles/{role['id']}/grants",
        json={"scope_type": "customer", "scope_id": customer.id, "capabilities": ["view"]},
    ).json()

    updated = admin_client.patch(
        f"/roles/{role['id']}/grants/{grant['id']}",
        json={"capabilities": ["view", "download"]},
    )
    assert updated.status_code == 200
    assert set(updated.json()["capabilities"]) == {"view", "download"}

    deleted = admin_client.delete(f"/roles/{role['id']}/grants/{grant['id']}")
    assert deleted.status_code == 204
    assert admin_client.get(f"/roles/{role['id']}/grants").json() == []


def test_source_scoped_grant_via_api(session, admin_client):
    customer = Customer(name="Vodacom Tanzania")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = Source(
        name="app01", customer_id=customer.id, protocol=Protocol.ssh, host="h", base_path="/var/log"
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    role = admin_client.post("/roles", json={"name": "Narrow"}).json()
    response = admin_client.post(
        f"/roles/{role['id']}/grants",
        json={"scope_type": "source", "scope_id": source.id, "capabilities": ["view"]},
    )
    assert response.status_code == 201
