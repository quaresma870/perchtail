"""Upgrading push agents from the portal (#319): the endpoints, with the
agent registry faked (no real connection)."""

import uuid

import pytest
from app import system_settings
from app.agent_registry import AgentCommandError, AgentTimeoutError
from app.agent_upgrades import TOO_OLD, get_agent_upgrades
from app.api import agent_upgrades as api
from app.api.agent_upgrades import router as agent_upgrades_router
from app.api.auth import get_current_active_user
from app.api.sources import router as sources_router
from app.auth.models import AuditLog, GlobalCapability, Role, User
from app.db import get_session
from app.models import Protocol, Source
from app.version import APP_VERSION
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


class FakeRegistry:
    """Stands in for the agent registry: which sources are connected, and
    how each agent answers the upgrade command."""

    def __init__(self):
        self.connected: set[int] = set()
        self.answers: dict[int, object] = {}
        self.sent: list[tuple[int, str, dict]] = []

    def is_connected(self, source_id):
        return source_id in self.connected

    def send_command_sync(self, source_id, command_type, timeout=30.0, **fields):
        self.sent.append((source_id, command_type, fields))
        answer = self.answers.get(source_id, {"type": "upgrade_accepted"})
        if isinstance(answer, Exception):
            raise answer
        return {**answer, "id": fields["command_id"]}


@pytest.fixture()
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(api, "get_agent_registry", lambda: fake)
    monkeypatch.setattr("app.api.sources.get_agent_registry", lambda: fake)
    return fake


def _user(session, *, super_admin=False, capabilities=()) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=super_admin,
        global_capabilities=list(capabilities),
    )
    session.add(role)
    session.commit()
    user = User(username=f"u-{uuid.uuid4().hex[:8]}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _client(session, user) -> TestClient:
    app = FastAPI()
    app.include_router(agent_upgrades_router)
    app.include_router(sources_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _agent(session, *, version="0.0.1", name=None, protocol=Protocol.agent) -> Source:
    source = Source(
        name=name or f"agent-{uuid.uuid4().hex[:6]}",
        protocol=protocol,
        host="host01",
        base_path="/var/log",
        agent_version=version,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _enable(session, on=True):
    system_settings.set_bool(session, "agent_remote_upgrade_enabled", on)
    session.commit()


@pytest.fixture()
def admin(session):
    return _client(session, _user(session, super_admin=True))


def _audits(session):
    return session.exec(select(AuditLog).where(AuditLog.action == "agent.upgrade")).all()


def test_the_setting_is_off_by_default(session, admin, registry):
    assert system_settings.get_bool(session, "agent_remote_upgrade_enabled") is False
    source = _agent(session)
    registry.connected.add(source.id)
    response = admin.post(f"/sources/{source.id}/agent-upgrade")
    assert response.status_code == 409
    assert "Settings → System" in response.json()["detail"]
    assert registry.sent == []


def test_an_outdated_connected_agent_is_asked_to_upgrade(session, admin, registry):
    _enable(session)
    source = _agent(session)
    registry.connected.add(source.id)

    response = admin.post(f"/sources/{source.id}/agent-upgrade")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == "accepted"
    assert body["upgrade"]["status"] == "running"
    assert body["upgrade"]["to_version"] == APP_VERSION

    # Only the version goes to the agent: never a URL.
    [(source_id, command, fields)] = registry.sent
    assert (source_id, command) == (source.id, "upgrade")
    assert {k: v for k, v in fields.items() if k != "command_id"} == {"version": APP_VERSION}

    [audit] = _audits(session)
    assert audit.target_id == source.id
    assert audit.event_metadata["from"] == "0.0.1"
    assert audit.event_metadata["to"] == APP_VERSION
    assert audit.event_metadata["result"] == "accepted"

    # The source page shows it.
    shown = admin.get(f"/sources/{source.id}").json()["agent_upgrade"]
    assert shown["status"] == "running"


def test_an_agent_too_old_for_the_command_is_reported(session, admin, registry):
    # An agent before 0.7 answers an unknown command with unknown_error.
    _enable(session)
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = AgentCommandError(
        'unknown command type "upgrade"', message_type="unknown_error"
    )
    response = admin.post(f"/sources/{source.id}/agent-upgrade")
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "too_old"
    assert body["detail"] == TOO_OLD
    assert body["upgrade"]["status"] == "failed"
    assert _audits(session)[0].event_metadata["result"] == "too_old"


def test_a_refusal_by_the_agent_is_reported(session, admin, registry):
    _enable(session)
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = AgentCommandError(
        "this host doesn't allow upgrades from the portal (PERCHTAIL_REMOTE_UPGRADE=false)",
        code="refused",
        message_type="upgrade_error",
    )
    body = admin.post(f"/sources/{source.id}/agent-upgrade").json()
    assert body["result"] == "refused"
    assert "PERCHTAIL_REMOTE_UPGRADE=false" in body["detail"]
    [audit] = _audits(session)
    assert audit.event_metadata["result"] == "refused"
    assert "PERCHTAIL_REMOTE_UPGRADE" in audit.event_metadata["reason"]


def test_an_agent_that_does_not_answer(session, admin, registry):
    _enable(session)
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = AgentTimeoutError("no answer")
    body = admin.post(f"/sources/{source.id}/agent-upgrade").json()
    assert body["result"] == "failed"
    assert "didn't answer" in body["detail"]


@pytest.mark.parametrize(
    ("version", "connected", "expected"),
    [
        ("0.0.1", False, "isn't connected"),
        (APP_VERSION, True, "isn't older than this server"),
        ("99.0.0", True, "isn't older than this server"),
        ("dev", True, "not a release"),
        (None, True, "too old to upgrade remotely"),
    ],
)
def test_agents_that_cannot_be_upgraded_are_refused(
    session, admin, registry, version, connected, expected
):
    _enable(session)
    source = _agent(session, version=version)
    if connected:
        registry.connected.add(source.id)
    response = admin.post(f"/sources/{source.id}/agent-upgrade")
    assert response.status_code == 409
    assert expected in response.json()["detail"]
    assert registry.sent == []
    assert _audits(session) == []


def test_only_agent_sources(session, admin, registry):
    _enable(session)
    source = _agent(session, protocol=Protocol.ssh)
    response = admin.post(f"/sources/{source.id}/agent-upgrade")
    assert response.status_code == 409
    assert admin.post("/sources/99999/agent-upgrade").status_code == 404


def test_one_upgrade_at_a_time(session, admin, registry):
    _enable(session)
    source = _agent(session)
    registry.connected.add(source.id)
    assert admin.post(f"/sources/{source.id}/agent-upgrade").json()["result"] == "accepted"
    body = admin.post(f"/sources/{source.id}/agent-upgrade").json()
    assert body["result"] == "busy"
    assert len(registry.sent) == 1


def test_users_who_cannot_manage_sources_cannot_upgrade(session, registry):
    _enable(session)
    source = _agent(session)
    registry.connected.add(source.id)
    viewer = _client(session, _user(session))
    assert viewer.post(f"/sources/{source.id}/agent-upgrade").status_code == 403
    assert viewer.post("/agent-upgrades", json={"source_ids": [source.id]}).status_code == 403
    assert viewer.get("/agent-upgrades").status_code == 403
    assert registry.sent == []

    manager = _client(session, _user(session, capabilities=[GlobalCapability.create_source]))
    assert manager.post(f"/sources/{source.id}/agent-upgrade").status_code == 200


def test_bulk_upgrade_asks_the_outdated_agents_and_skips_the_rest(session, admin, registry):
    _enable(session)
    outdated = _agent(session, name="outdated")
    current = _agent(session, version=APP_VERSION, name="current")
    offline = _agent(session, name="offline")
    old = _agent(session, name="old")
    for source in (outdated, current, old):
        registry.connected.add(source.id)
    registry.answers[old.id] = AgentCommandError("unknown", message_type="unknown_error")

    response = admin.post(
        "/agent-upgrades",
        json={"source_ids": [outdated.id, current.id, offline.id, old.id, 424242]},
    )
    assert response.status_code == 200
    results = {r["source_id"]: r for r in response.json()}
    assert results[outdated.id]["result"] == "accepted"
    assert results[current.id]["result"] == "skipped"
    assert results[offline.id]["result"] == "skipped"
    assert "isn't connected" in results[offline.id]["detail"]
    assert results[old.id]["result"] == "too_old"
    assert results[424242]["result"] == "skipped"
    assert {s for s, _c, _f in registry.sent} == {outdated.id, old.id}
    assert {a.target_id for a in _audits(session)} == {outdated.id, old.id}


def test_bulk_upgrade_needs_the_setting(session, admin, registry):
    source = _agent(session)
    registry.connected.add(source.id)
    response = admin.post("/agent-upgrades", json={"source_ids": [source.id]})
    assert response.status_code == 409


def test_availability_says_whether_binaries_are_bundled(session, admin, registry, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "agent_binaries_dir", "")
    body = admin.get("/agent-upgrades").json()
    assert body == {
        "enabled": False,
        "version": APP_VERSION,
        "bundled": False,
        "bundled_reason": body["bundled_reason"],
    }
    assert body["bundled_reason"]
    _enable(session)
    assert admin.get("/agent-upgrades").json()["enabled"] is True


def test_the_progress_reaches_the_source_page(session, admin, registry):
    _enable(session)
    source = _agent(session)
    registry.connected.add(source.id)
    admin.post(f"/sources/{source.id}/agent-upgrade")
    [(_s, _c, fields)] = registry.sent
    get_agent_upgrades().handle_message(
        source.id,
        {"type": "upgrade_progress", "id": fields["command_id"], "stage": "restarting"},
    )
    assert admin.get(f"/sources/{source.id}").json()["agent_upgrade"]["status"] == "restarting"
    get_agent_upgrades().agent_connected(source.id, APP_VERSION)
    shown = admin.get(f"/sources/{source.id}").json()["agent_upgrade"]
    assert shown["status"] == "succeeded"


def test_the_setting_can_be_switched_from_system_settings(session):
    from app.api.system_settings import router as settings_router

    app = FastAPI()
    app.include_router(settings_router)
    app.dependency_overrides[get_session] = lambda: session
    user = _user(session, super_admin=True)
    app.dependency_overrides[get_current_active_user] = lambda: user
    client = TestClient(app)
    assert client.get("/system-settings").json()["agent_remote_upgrade_enabled"] is False
    response = client.patch("/system-settings", json={"agent_remote_upgrade_enabled": True})
    assert response.status_code == 200
    assert response.json()["agent_remote_upgrade_enabled"] is True
