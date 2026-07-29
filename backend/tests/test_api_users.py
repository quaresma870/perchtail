import pytest
from app.api.auth import get_current_active_user
from app.api.users import router as users_router
from app.auth.models import GlobalCapability, Role, User
from app.auth.providers.local import verify_password
from app.db import get_session
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


def _make_target_role(session, name="Support") -> Role:
    role = Role(name=name)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(users_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


@pytest.fixture()
def admin_client(session, client_for):
    return client_for(_make_user(session, is_super_admin=True))


def test_admin_can_create_user_forces_password_change(session, admin_client):
    role = _make_target_role(session)
    response = admin_client.post(
        "/users", json={"username": "jdoe@example.com", "password": "s3cret!", "role_id": role.id}
    )
    assert response.status_code == 201
    assert response.json()["must_change_password"] is True


def test_plain_user_cannot_manage_users(session, client_for):
    user = _make_user(session)
    client = client_for(user)
    assert client.get("/users").status_code == 403


def test_manage_users_capability_sufficient(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.manage_users])
    client = client_for(user)
    role = _make_target_role(session)

    response = client.post(
        "/users", json={"username": "a@example.com", "password": "pw123456", "role_id": role.id}
    )
    assert response.status_code == 201


def test_duplicate_username_rejected(session, admin_client):
    role = _make_target_role(session)
    admin_client.post(
        "/users", json={"username": "dup@example.com", "password": "s3cret!", "role_id": role.id}
    )
    response = admin_client.post(
        "/users", json={"username": "dup@example.com", "password": "s3cret!", "role_id": role.id}
    )
    assert response.status_code == 409


def test_create_user_requires_existing_role(session, admin_client):
    response = admin_client.post(
        "/users", json={"username": "a@example.com", "password": "s3cret!", "role_id": 999999}
    )
    assert response.status_code == 404


def test_update_user_role_and_active(session, admin_client):
    role_a = _make_target_role(session, "A")
    role_b = _make_target_role(session, "B")
    created = admin_client.post(
        "/users", json={"username": "a@example.com", "password": "s3cret!", "role_id": role_a.id}
    ).json()

    response = admin_client.patch(
        f"/users/{created['id']}", json={"role_id": role_b.id, "active": False}
    )
    assert response.status_code == 200
    assert response.json()["role_id"] == role_b.id
    assert response.json()["active"] is False


def test_reset_password_forces_change_and_returns_temp_password(session, admin_client):
    role = _make_target_role(session)
    created = admin_client.post(
        "/users", json={"username": "a@example.com", "password": "s3cret!", "role_id": role.id}
    ).json()

    response = admin_client.post(f"/users/{created['id']}/reset-password")
    assert response.status_code == 200
    temp_password = response.json()["temporary_password"]
    assert len(temp_password) > 8

    target = session.get(User, created["id"])
    assert target.must_change_password is True
    assert verify_password(target, temp_password) is True


def test_deactivate_is_soft_delete(session, admin_client):
    role = _make_target_role(session)
    created = admin_client.post(
        "/users", json={"username": "a@example.com", "password": "s3cret!", "role_id": role.id}
    ).json()

    response = admin_client.delete(f"/users/{created['id']}")
    assert response.status_code == 204

    target = session.get(User, created["id"])
    assert target is not None
    assert target.active is False
