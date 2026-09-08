import uuid
from datetime import timedelta

import pytest
from app.api.audit import router as audit_router
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, Role, User
from app.db import get_session
from app.timeutils import utcnow
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
        app.include_router(audit_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def _seed(session, viewer_id, *, count=1, action="source.create", target_type="source", **kw):
    entries = []
    for i in range(count):
        entries.append(
            record_audit_event(
                session,
                user_id=viewer_id,
                action=action,
                target_type=target_type,
                target_id=i,
                **kw,
            )
        )
    session.commit()
    return entries


def test_plain_user_cannot_read_audit_log(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    response = client.get("/audit")
    assert response.status_code == 403


def test_capability_holder_can_read_audit_log(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.view_audit_log])
    _seed(session, user.id, count=3)
    client = client_for(user)

    response = client.get("/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert body["items"][0]["username"] == user.username


def test_super_admin_can_read_without_the_explicit_capability(session, client_for):
    admin = _make_user(session, is_super_admin=True)
    _seed(session, admin.id, count=1)
    client = client_for(admin)

    response = client.get("/audit")
    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_manage_system_settings_alone_does_not_grant_audit_read(session, client_for):
    # view_audit_log is deliberately its own capability, never implied by
    # another one -- see GlobalCapability's docstring in auth/models.py.
    user = _make_user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    client = client_for(user)

    response = client.get("/audit")
    assert response.status_code == 403


def test_pagination_limit_and_offset(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=5)
    client = client_for(user)

    response = client.get("/audit", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_filter_by_exact_action(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    _seed(session, user.id, count=1, action="user.login", target_type=None)
    client = client_for(user)

    response = client.get("/audit", params={"action": "user.login"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "user.login"


def test_filter_by_action_prefix_wildcard(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    _seed(session, user.id, count=1, action="source.delete", target_type="source")
    _seed(session, user.id, count=1, action="user.login", target_type=None)
    client = client_for(user)

    response = client.get("/audit", params={"action": "source.*"})
    body = response.json()
    assert body["total"] == 2
    assert {item["action"] for item in body["items"]} == {"source.create", "source.delete"}


def test_action_prefix_wildcard_does_not_treat_underscore_as_a_sql_wildcard(session, client_for):
    # "role_grant.*" strips to the prefix "role_grant." -- SQL LIKE treats a
    # literal "_" as a single-character wildcard unless escaped, which would
    # make this prefix also match an unrelated action like "roleXgrant.foo".
    # _action_condition's startswith(..., autoescape=True) must prevent that.
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="role_grant.create", target_type="role_grant")
    _seed(session, user.id, count=1, action="roleXgrant.create", target_type=None)
    client = client_for(user)

    response = client.get("/audit", params={"action": "role_grant.*"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "role_grant.create"


def test_filter_by_target_type(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="rule.create", target_type="rule")
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    client = client_for(user)

    response = client.get("/audit", params={"target_type": "rule"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["target_type"] == "rule"


def test_filter_by_user_id(session, client_for):
    user = _make_user(session, is_super_admin=True)
    other = _make_user(session)
    _seed(session, user.id, count=1)
    _seed(session, other.id, count=1)
    client = client_for(user)

    response = client.get("/audit", params={"user_id": other.id})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["user_id"] == other.id


def test_filter_by_date_range(session, client_for):
    user = _make_user(session, is_super_admin=True)
    old_entry = record_audit_event(session, user_id=user.id, action="source.create")
    session.commit()
    old_entry.timestamp = utcnow() - timedelta(days=10)
    session.add(old_entry)
    session.commit()

    _seed(session, user.id, count=1, action="source.update")
    client = client_for(user)

    since = (utcnow() - timedelta(days=1)).isoformat()
    response = client.get("/audit", params={"since": since})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "source.update"


def test_filters_endpoint_returns_distinct_actions_and_target_types(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    _seed(session, user.id, count=1, action="source.create", target_type="source")
    _seed(session, user.id, count=1, action="user.login", target_type=None)
    client = client_for(user)

    response = client.get("/audit/filters")
    assert response.status_code == 200
    body = response.json()
    assert body["actions"] == ["source.create", "user.login"]
    assert body["target_types"] == ["source"]


def test_metadata_is_returned(session, client_for):
    user = _make_user(session, is_super_admin=True)
    _seed(session, user.id, count=1, metadata={"name": "app01"})
    client = client_for(user)

    response = client.get("/audit")
    assert response.json()["items"][0]["metadata"] == {"name": "app01"}
