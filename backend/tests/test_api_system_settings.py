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
    "audit_folder_listings": True,
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
    "credential_max_age_days": 90,
    "concurrency_global": 16,
    "concurrency_per_source": 4,
    "concurrency_user_transfers": 2,
    "listing_max_entries": 5000,
    "open_from_end_mb": 50,
    "backup_interval_hours": 24,
    "redaction_enabled": False,
    "redaction_downloads": True,
    "redaction_download_suffix": True,
    "require_mfa_local_accounts": False,
    "password_reset_enabled": False,
    "password_reset_minutes": 30,
    "password_reset_unavailable": "Set up email under Settings → Email and turn it on first",
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

    response = client.patch("/system-settings", json={"audit_retention_days": 90})
    assert response.status_code == 200
    assert response.json()["audit_retention_days"] == 90

    response = client.get("/system-settings")
    assert response.json()["audit_retention_days"] == 90


@pytest.mark.parametrize("days", [1, 30, 89])
def test_audit_retention_below_the_minimum_is_refused(session, client_for, days):
    client = client_for(_make_user(session, is_super_admin=True))
    response = client.patch("/system-settings", json={"audit_retention_days": days})
    assert response.status_code == 422
    assert "90 days" in response.json()["detail"]
    assert client.get("/system-settings").json()["audit_retention_days"] == 365


@pytest.mark.parametrize(("current", "new"), [(365, 90), (0, 365), (0, 90)])
def test_only_a_super_admin_shortens_the_audit_retention(session, client_for, current, new):
    from app import system_settings
    from app.auth.models import AuditLog
    from sqlmodel import select

    system_settings.set_int(session, "audit_retention_days", current)
    session.commit()
    delegated = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])

    response = client_for(delegated).patch("/system-settings", json={"audit_retention_days": new})
    assert response.status_code == 403
    assert system_settings.get_int(session, "audit_retention_days") == current
    assert session.exec(select(AuditLog)).all() == []

    admin = _make_user(session, is_super_admin=True)
    response = client_for(admin).patch("/system-settings", json={"audit_retention_days": new})
    assert response.status_code == 200
    assert response.json()["audit_retention_days"] == new


@pytest.mark.parametrize(("current", "new"), [(365, 400), (365, 0), (90, 365)])
def test_a_delegated_admin_may_lengthen_the_audit_retention(session, client_for, current, new):
    from app import system_settings

    system_settings.set_int(session, "audit_retention_days", current)
    session.commit()
    delegated = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    response = client_for(delegated).patch("/system-settings", json={"audit_retention_days": new})
    assert response.status_code == 200
    assert response.json()["audit_retention_days"] == new


def test_only_a_super_admin_turns_off_folder_listing_auditing(session, client_for):
    from app import system_settings
    from app.auth.models import AuditLog
    from sqlmodel import select

    delegated = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    response = client_for(delegated).patch(
        "/system-settings", json={"audit_folder_listings": False}
    )
    assert response.status_code == 403
    assert system_settings.get_bool(session, "audit_folder_listings") is True
    assert session.exec(select(AuditLog)).all() == []

    admin = _make_user(session, is_super_admin=True)
    response = client_for(admin).patch("/system-settings", json={"audit_folder_listings": False})
    assert response.status_code == 200
    assert response.json()["audit_folder_listings"] is False


def test_a_delegated_admin_may_turn_folder_listing_auditing_on(session, client_for):
    from app import system_settings

    system_settings.set_bool(session, "audit_folder_listings", False)
    session.commit()
    delegated = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    client = client_for(delegated)

    response = client.patch("/system-settings", json={"audit_folder_listings": True})
    assert response.status_code == 200
    assert response.json()["audit_folder_listings"] is True
    # Sending the value it already has changes nothing and is not refused.
    response = client.patch("/system-settings", json={"audit_folder_listings": True})
    assert response.status_code == 200


@pytest.mark.parametrize("start", [False, True])
def test_only_a_super_admin_changes_whether_local_accounts_need_mfa(session, client_for, start):
    from app import system_settings
    from app.auth.models import AuditLog
    from sqlmodel import select

    system_settings.set_bool(session, "require_mfa_local_accounts", start)
    session.commit()
    delegated = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    client = client_for(delegated)
    response = client.patch("/system-settings", json={"require_mfa_local_accounts": not start})
    assert response.status_code == 403
    assert system_settings.get_bool(session, "require_mfa_local_accounts") is start
    assert session.exec(select(AuditLog)).all() == []
    # Sending the value it already has changes nothing and is not refused.
    response = client.patch("/system-settings", json={"require_mfa_local_accounts": start})
    assert response.status_code == 200

    admin = _make_user(session, is_super_admin=True)
    response = client_for(admin).patch(
        "/system-settings", json={"require_mfa_local_accounts": not start}
    )
    assert response.status_code == 200
    assert response.json()["require_mfa_local_accounts"] is (not start)
    entry = session.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert entry.action == "system_settings.update"
    assert entry.event_metadata["require_mfa_local_accounts"] == {"old": start, "new": not start}


def test_the_audit_entry_records_old_and_new_values(session, client_for):
    from app.auth.models import AuditLog
    from sqlmodel import select

    client = client_for(_make_user(session, is_super_admin=True))
    response = client.patch(
        "/system-settings",
        json={"audit_retention_days": 400, "audit_folder_listings": False},
    )
    assert response.status_code == 200
    [entry] = session.exec(
        select(AuditLog).where(AuditLog.action == "system_settings.update")
    ).all()
    assert entry.event_metadata["audit_retention_days"] == {"old": 365, "new": 400}
    assert entry.event_metadata["audit_folder_listings"] == {"old": True, "new": False}


def test_open_from_end_defaults_to_50_and_its_change_is_audited(session, client_for):
    from app import system_settings

    """Files above `open_from_end_mb` open at their end in the viewer: 50 MB
    unless changed, 0 turns it off, and a change is audited like the
    other settings, with the values before and after."""
    from app.auth.models import AuditLog
    from sqlmodel import select

    admin = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    client = client_for(admin)
    assert client.get("/system-settings").json()["open_from_end_mb"] == 50
    assert system_settings.get_int(session, "open_from_end_mb") == 50

    response = client.patch("/system-settings", json={"open_from_end_mb": 0})
    assert response.status_code == 200
    assert response.json()["open_from_end_mb"] == 0
    assert system_settings.get_int(session, "open_from_end_mb") == 0
    [entry] = session.exec(
        select(AuditLog).where(AuditLog.action == "system_settings.update")
    ).all()
    assert entry.event_metadata["open_from_end_mb"] == {"old": 50, "new": 0}


def test_a_secret_setting_is_recorded_only_as_changed(session, client_for, monkeypatch):
    from app.auth.models import AuditLog
    from sqlmodel import select

    monkeypatch.setattr(
        "app.api.system_settings.SECRET_SETTINGS", frozenset({"audit_retention_days"})
    )
    client = client_for(_make_user(session, is_super_admin=True))
    assert client.patch("/system-settings", json={"audit_retention_days": 400}).status_code == 200
    [entry] = session.exec(
        select(AuditLog).where(AuditLog.action == "system_settings.update")
    ).all()
    assert entry.event_metadata["audit_retention_days"] == "changed"


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


@pytest.mark.parametrize(
    "payload, status",
    [
        ({"concurrency_global": 32, "concurrency_per_source": 8}, 200),
        ({"concurrency_user_transfers": 5}, 200),
        ({"concurrency_global": 0}, 422),
        ({"concurrency_global": 257}, 422),
        ({"concurrency_per_source": 65}, 422),
        ({"concurrency_user_transfers": 0}, 422),
        # More per source than in all makes no sense (#344).
        ({"concurrency_per_source": 20}, 422),
        ({"concurrency_global": 2}, 422),
        ({"listing_max_entries": 100}, 200),
        ({"listing_max_entries": 99}, 422),
        ({"listing_max_entries": 20001}, 422),
        # Opening large files at their end: 0 turns it off.
        ({"open_from_end_mb": 0}, 200),
        ({"open_from_end_mb": 1}, 200),
        ({"open_from_end_mb": 100000}, 200),
        ({"open_from_end_mb": -1}, 422),
        ({"open_from_end_mb": 100001}, 422),
        ({"open_from_end_mb": 1.5}, 422),
        ({"backup_interval_hours": 24}, 200),
        ({"backup_interval_hours": 168}, 200),
        ({"backup_interval_hours": -1}, 422),
        ({"backup_interval_hours": 169}, 422),
    ],
)
def test_limits_are_kept_within_their_ranges(session, client_for, payload, status):
    client = client_for(_make_user(session, is_super_admin=True))
    response = client.patch("/system-settings", json=payload)
    assert response.status_code == status
    current = client.get("/system-settings").json()
    for key, value in payload.items():
        assert (current[key] == value) is (status == 200)
