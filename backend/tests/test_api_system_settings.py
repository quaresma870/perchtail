import uuid

import pytest
from app.api.auth import get_current_active_user
from app.api.system_settings import router as system_settings_router
from app.auth.models import GlobalCapability, Role, User
from app.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient


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
        app.include_router(system_settings_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


_DEFAULTS = {
    "audit_view_enabled": True,
    "audit_retention_days": 365,
    "source_max_roots": 20,
    "connections_view_switchable": True,
    "connections_view_default": "list",
    "update_check_enabled": True,
    "password_min_length": 12,
    "password_max_length": 128,
    "password_reject_simple": True,
    "password_reject_personal": True,
    "password_history": 0,
    "password_required_classes": 0,
    "password_max_age_days": 0,
    "explorer_sort_default": "name_asc",
    "explorer_refresh_seconds": 30,
    "tail_interval_seconds": 2,
    "source_check_minutes": 10,
    "name_index_enabled": True,
    "name_index_minutes": 15,
    "alerts_interval_seconds": 60,
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

    response = client.patch("/system-settings", json={"audit_view_enabled": False})
    assert response.status_code == 403


def test_admin_capability_can_toggle_audit_view(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    client = client_for(user)

    response = client.patch("/system-settings", json={"audit_view_enabled": False})
    assert response.status_code == 200
    assert response.json() == {**_DEFAULTS, "audit_view_enabled": False}

    # Persists for a fresh read, not just echoed back from the PATCH response.
    response = client.get("/system-settings")
    assert response.json() == {**_DEFAULTS, "audit_view_enabled": False}


def test_super_admin_can_toggle_without_the_explicit_capability(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    response = client.patch("/system-settings", json={"audit_view_enabled": False})
    assert response.status_code == 200
    assert response.json()["audit_view_enabled"] is False


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


def test_connections_view_settings_default_and_update(session, client_for):
    admin_client = client_for(_make_user(session, is_super_admin=True))
    got = admin_client.get("/system-settings").json()
    assert got["connections_view_default"] == "list"
    assert got["connections_view_switchable"] is True

    updated = admin_client.patch(
        "/system-settings",
        json={"connections_view_default": "tree", "connections_view_switchable": False},
    ).json()

    assert updated["connections_view_default"] == "tree"
    assert updated["connections_view_switchable"] is False


def test_connections_view_default_rejects_unknown_values(session, client_for):
    admin_client = client_for(_make_user(session, is_super_admin=True))
    response = admin_client.patch("/system-settings", json={"connections_view_default": "grid"})
    assert response.status_code == 422


@pytest.mark.parametrize("value, status", [(1, 200), (100, 200), (0, 422), (101, 422)])
def test_source_max_roots_is_bounded_by_the_hard_cap(session, client_for, value, status):
    client = client_for(_make_user(session, is_super_admin=True))
    response = client.patch("/system-settings", json={"source_max_roots": value})
    assert response.status_code == status
    if status == 200:
        assert client.get("/system-settings").json()["source_max_roots"] == value


@pytest.mark.parametrize("seconds", [1, 2, 5, 10])
def test_tail_interval_is_one_of_the_offered_values(session, client_for, seconds):
    # #283: a System setting, not each user's.
    client = client_for(_make_user(session, is_super_admin=True))

    response = client.patch("/system-settings", json={"tail_interval_seconds": seconds})
    assert response.status_code == 200
    assert client.get("/system-settings").json()["tail_interval_seconds"] == seconds


@pytest.mark.parametrize("seconds", [0, 3, 30, -1])
def test_tail_interval_refuses_other_values(session, client_for, seconds):
    client = client_for(_make_user(session, is_super_admin=True))

    response = client.patch("/system-settings", json={"tail_interval_seconds": seconds})
    assert response.status_code == 422
    assert client.get("/system-settings").json()["tail_interval_seconds"] == 2


@pytest.mark.parametrize("minutes", [4, 1441])
def test_name_index_interval_is_kept_between_5_minutes_and_a_day(session, client_for, minutes):
    admin = _make_user(session, is_super_admin=True)
    response = client_for(admin).patch("/system-settings", json={"name_index_minutes": minutes})
    assert response.status_code == 422
