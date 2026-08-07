import pytest
from app.api.auth import get_current_active_user
from app.api.system_settings import router as system_settings_router
from app.auth.models import GlobalCapability, Role, User
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


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(system_settings_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def test_defaults_readable_by_any_authenticated_user(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.get("/system-settings")
    assert response.status_code == 200
    assert response.json() == {"search_view_enabled": True}


def test_plain_user_cannot_update_settings(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.patch("/system-settings", json={"search_view_enabled": False})
    assert response.status_code == 403


def test_admin_capability_can_toggle_search_view(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    client = client_for(user)

    response = client.patch("/system-settings", json={"search_view_enabled": False})
    assert response.status_code == 200
    assert response.json() == {"search_view_enabled": False}

    # Persists for a fresh read, not just echoed back from the PATCH response.
    response = client.get("/system-settings")
    assert response.json() == {"search_view_enabled": False}


def test_super_admin_can_toggle_without_the_explicit_capability(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    response = client.patch("/system-settings", json={"search_view_enabled": False})
    assert response.status_code == 200
    assert response.json()["search_view_enabled"] is False


def test_partial_update_leaves_other_settings_unchanged(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    response = client.patch("/system-settings", json={})
    assert response.status_code == 200
    assert response.json() == {"search_view_enabled": True}
