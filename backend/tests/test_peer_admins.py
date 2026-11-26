"""A delegated admin can't take over another admin: only a super admin
resets the password of, deactivates or changes the role of an account whose
role manages users or roles (app/api/users.py). Through the whole app, so
the refusal's audit entry is checked too."""

import uuid

import pytest
from app.api.auth import session_cookie_name
from app.auth.models import AuditLog, GlobalCapability, Role, User
from app.auth.sessions import create_session
from app.db import get_session
from app.main import app
from fastapi.testclient import TestClient
from sqlmodel import select


def _role(session, capabilities=(), *, super_admin=False) -> Role:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=super_admin,
        global_capabilities=list(capabilities),
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def _user(session, role: Role) -> User:
    user = User(username=f"user-{uuid.uuid4().hex}", role_id=role.id, password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def real_app(session):
    app.dependency_overrides[get_session] = lambda: session
    yield
    app.dependency_overrides.pop(get_session, None)


def _client(session, user) -> TestClient:
    _, token = create_session(session, user, user_agent="pytest-agent")
    client = TestClient(app, client=("127.0.0.1", 50000))
    client.cookies.set(session_cookie_name(), token)
    return client


def _denials(session) -> list[AuditLog]:
    session.expire_all()
    return list(session.exec(select(AuditLog).where(AuditLog.action == "access.denied")).all())


@pytest.mark.parametrize(
    "peer_capability", [GlobalCapability.manage_users, GlobalCapability.manage_roles]
)
def test_a_delegated_admin_cannot_take_over_a_peer(session, real_app, peer_capability):
    admins = _role(session, [GlobalCapability.manage_users, GlobalCapability.manage_roles])
    actor = _user(session, admins)
    # The peer's role is within the actor's authority: only the new rule stops it.
    peer = _user(session, _role(session, [peer_capability]))
    plain = _role(session)
    client = _client(session, actor)

    attempts = [
        client.post(f"/users/{peer.id}/reset-password"),
        client.delete(f"/users/{peer.id}"),
        client.patch(f"/users/{peer.id}", json={"active": False}),
        client.patch(f"/users/{peer.id}", json={"role_id": plain.id}),
    ]
    for response in attempts:
        assert response.status_code == 403, response.text
        assert response.json() == {"detail": "Not permitted"}

    session.expire_all()
    unchanged = session.get(User, peer.id)
    assert unchanged.active and unchanged.role_id != plain.id
    assert unchanged.password_hash == "x" and not unchanged.must_change_password
    denied = _denials(session)
    assert len(denied) == len(attempts)
    assert {(d.user_id, d.target_type, d.target_id) for d in denied} == {
        (actor.id, "user", peer.id)
    }
    assert {d.event_metadata["reason"] for d in denied} == {"peer_admin"}


def test_a_delegated_admin_still_manages_other_accounts(session, real_app):
    actor = _user(session, _role(session, [GlobalCapability.manage_users]))
    target = _user(session, _role(session))
    client = _client(session, actor)

    assert client.post(f"/users/{target.id}/reset-password").status_code == 200
    plain = _role(session)
    assert client.patch(f"/users/{target.id}", json={"role_id": plain.id}).status_code == 200
    assert client.delete(f"/users/{target.id}").status_code == 204


def test_a_super_admin_can_act_on_any_admin(session, real_app):
    actor = _user(session, _role(session, super_admin=True))
    peer = _user(session, _role(session, [GlobalCapability.manage_users]))
    client = _client(session, actor)

    assert client.post(f"/users/{peer.id}/reset-password").status_code == 200
    assert client.delete(f"/users/{peer.id}").status_code == 204
    assert _denials(session) == []
