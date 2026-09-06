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


_DEFAULTS = {
    "search_view_enabled": True,
    "audit_view_enabled": True,
    "audit_retention_days": 365,
}


def test_defaults_readable_by_any_authenticated_user(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.get("/system-settings")
    assert response.status_code == 200
    assert response.json() == _DEFAULTS


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
    assert response.json() == {**_DEFAULTS, "search_view_enabled": False}

    # Persists for a fresh read, not just echoed back from the PATCH response.
    response = client.get("/system-settings")
    assert response.json() == {**_DEFAULTS, "search_view_enabled": False}


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
    assert response.json() == _DEFAULTS


def test_audit_retention_days_can_be_updated(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    response = client.patch("/system-settings", json={"audit_retention_days": 30})
    assert response.status_code == 200
    assert response.json()["audit_retention_days"] == 30

    response = client.get("/system-settings")
    assert response.json()["audit_retention_days"] == 30


def test_audit_retention_days_zero_means_keep_forever_and_is_accepted(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    response = client.patch("/system-settings", json={"audit_retention_days": 0})
    assert response.status_code == 200
    assert response.json()["audit_retention_days"] == 0


def test_audit_retention_days_rejects_negative(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    response = client.patch("/system-settings", json={"audit_retention_days": -1})
    assert response.status_code == 422


def test_explicit_null_is_ignored_not_stored_as_the_string_none(session, client_for):
    # exclude_unset still lets an explicit `null` through the Pydantic model
    # (it was "set", just to nothing) -- previously this reached
    # set_int()/set_bool() as a literal None, which stringified into a
    # SystemSetting.value no later get_int()/get_bool() call could parse
    # back (int("None") raises). It should just be a no-op instead.
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    response = client.patch("/system-settings", json={"audit_retention_days": None})
    assert response.status_code == 200
    assert response.json()["audit_retention_days"] == 365

    # And a later read doesn't crash either -- the real symptom before this
    # fix was every subsequent GET (and the daily purge sweep) raising.
    response = client.get("/system-settings")
    assert response.status_code == 200
    assert response.json()["audit_retention_days"] == 365
