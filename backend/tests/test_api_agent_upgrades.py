"""Settings -> Agents (#319): the endpoints, with the agent registry faked
(no real connection)."""

import uuid

import pytest
from app import agent_upgrades, system_settings
from app.agent_registry import AgentCommandError, AgentTimeoutError
from app.agent_upgrades import TOO_OLD, handle_message
from app.api import agent_upgrades as api
from app.api.agent_upgrades import POLICY_OFF
from app.api.agent_upgrades import router as agent_upgrades_router
from app.api.auth import get_current_active_user
from app.api.sources import MANAGE_EXISTING_SOURCE_REFUSED
from app.api.sources import router as sources_router
from app.auth.models import (
    AuditLog,
    Capability,
    GlobalCapability,
    Role,
    RoleGrant,
    ScopeType,
    User,
)
from app.db import get_session
from app.models import AgentUpgradeRun, Protocol, Source, SystemSetting
from app.version import APP_VERSION
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


class FakeRegistry:
    """Which sources are connected, and how each agent answers."""

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
    monkeypatch.setattr(agent_upgrades, "get_agent_registry", lambda: fake)
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


def _agent(
    session, *, version="0.0.1", name=None, protocol=Protocol.agent, policy="inherit", failures=0
) -> Source:
    source = Source(
        name=name or f"agent-{uuid.uuid4().hex[:6]}",
        protocol=protocol,
        host="host01",
        base_path="/var/log",
        agent_version=version,
        agent_upgrade_policy=policy,
        agent_upgrade_failures=failures,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _policy(session, policy="manual"):
    system_settings.set_choice(session, "agent_upgrade_policy", policy)
    session.commit()


@pytest.fixture()
def admin_user(session):
    return _user(session, super_admin=True)


@pytest.fixture()
def admin(session, admin_user):
    return _client(session, admin_user)


def _audits(session, action="agent.upgrade"):
    return session.exec(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    ).all()


# --- upgrading one agent --------------------------------------------------------


def test_upgrades_are_off_by_default(session, admin, registry):
    source = _agent(session)
    registry.connected.add(source.id)
    response = admin.post(f"/sources/{source.id}/agent-upgrade")
    assert response.status_code == 409
    assert response.json()["detail"] == POLICY_OFF
    assert registry.sent == []


def test_an_outdated_connected_agent_is_asked_to_upgrade(session, admin, admin_user, registry):
    _policy(session)
    source = _agent(session)
    registry.connected.add(source.id)

    response = admin.post(f"/sources/{source.id}/agent-upgrade")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == "accepted"
    run = body["run"]
    assert run["status"] == "accepted"
    assert run["to_version"] == APP_VERSION
    assert run["trigger"] == "manual"
    assert run["requested_by"] == admin_user.username

    # Only the version goes to the agent: never a URL.
    [(source_id, command, fields)] = registry.sent
    assert (source_id, command) == (source.id, "upgrade")
    assert {k: v for k, v in fields.items() if k != "command_id"} == {"version": APP_VERSION}

    [audit] = _audits(session)
    assert audit.user_id == admin_user.id
    assert audit.target_id == source.id
    assert audit.event_metadata["from"] == "0.0.1"
    assert audit.event_metadata["to"] == APP_VERSION
    assert audit.event_metadata["result"] == "accepted"
    assert audit.event_metadata["trigger"] == "manual"

    # Stored.
    [stored] = session.exec(select(AgentUpgradeRun)).all()
    assert stored.requested_by_user_id == admin_user.id


def test_a_sources_own_policy_wins(session, admin, registry):
    # System off, source manual: allowed. System manual, source off: not.
    own = _agent(session, policy="manual")
    registry.connected.add(own.id)
    assert admin.post(f"/sources/{own.id}/agent-upgrade").json()["result"] == "accepted"

    _policy(session, "manual")
    off = _agent(session, policy="off")
    registry.connected.add(off.id)
    response = admin.post(f"/sources/{off.id}/agent-upgrade")
    assert response.status_code == 409
    assert response.json()["detail"] == POLICY_OFF


def test_the_old_setting_still_allows_manual_upgrades(session, admin, registry):
    session.add(SystemSetting(key="agent_remote_upgrade_enabled", value="true"))
    session.commit()
    source = _agent(session)
    registry.connected.add(source.id)
    assert admin.get("/agent-upgrades").json()["policy"] == "manual"
    assert admin.post(f"/sources/{source.id}/agent-upgrade").json()["result"] == "accepted"


def test_an_agent_too_old_for_the_command_is_reported(session, admin, registry):
    _policy(session)
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = AgentCommandError(
        'unknown command type "upgrade"', message_type="unknown_error"
    )
    body = admin.post(f"/sources/{source.id}/agent-upgrade").json()
    assert body["result"] == "too_old"
    assert body["detail"] == TOO_OLD
    assert body["run"]["status"] == "too_old"
    assert _audits(session)[0].event_metadata["result"] == "too_old"


def test_a_refusal_by_the_agent_is_reported(session, admin, registry):
    # PERCHTAIL_REMOTE_UPGRADE=false on the host wins over any policy.
    _policy(session, "automatic")
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
    assert body["run"]["status"] == "refused"
    [audit] = _audits(session)
    assert audit.event_metadata["result"] == "refused"
    assert "PERCHTAIL_REMOTE_UPGRADE" in audit.event_metadata["reason"]


def test_an_agent_that_does_not_answer(session, admin, registry):
    _policy(session)
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
    _policy(session)
    source = _agent(session, version=version)
    if connected:
        registry.connected.add(source.id)
    response = admin.post(f"/sources/{source.id}/agent-upgrade")
    assert response.status_code == 409
    assert expected in response.json()["detail"]
    assert registry.sent == []
    assert _audits(session) == []


def test_only_agent_sources(session, admin, registry):
    _policy(session)
    source = _agent(session, protocol=Protocol.ssh)
    assert admin.post(f"/sources/{source.id}/agent-upgrade").status_code == 409
    assert admin.post("/sources/99999/agent-upgrade").status_code == 404
    assert admin.get(f"/sources/{source.id}/agent-upgrades").status_code == 409


def test_one_upgrade_at_a_time(session, admin, registry):
    _policy(session)
    source = _agent(session)
    registry.connected.add(source.id)
    assert admin.post(f"/sources/{source.id}/agent-upgrade").json()["result"] == "accepted"
    body = admin.post(f"/sources/{source.id}/agent-upgrade").json()
    assert body["result"] == "busy"
    assert len(registry.sent) == 1


def test_a_suspended_agent_can_still_be_upgraded_by_hand(session, admin, registry):
    _policy(session, "automatic")
    source = _agent(session, failures=3)
    registry.connected.add(source.id)
    assert admin.post(f"/sources/{source.id}/agent-upgrade").json()["result"] == "accepted"
    [run] = session.exec(select(AgentUpgradeRun)).all()
    handle_message(session, source.id, {"type": "upgrade_failed", "id": run.command_id})
    session.commit()
    session.expire_all()
    # A failure adds to the count.
    assert session.get(Source, source.id).agent_upgrade_failures == 4


# --- permissions -----------------------------------------------------------------


def test_users_who_cannot_manage_sources_cannot_use_any_of_it(session, registry):
    _policy(session)
    source = _agent(session)
    registry.connected.add(source.id)
    viewer = _client(session, _user(session))
    for method, url, body in (
        ("post", f"/sources/{source.id}/agent-upgrade", None),
        ("post", "/agent-upgrades", {}),
        ("get", "/agent-upgrades", None),
        ("patch", "/agent-upgrades/settings", {"policy": "automatic"}),
        ("get", "/agent-upgrades/agents", None),
        ("put", f"/sources/{source.id}/agent-upgrade-policy", {"policy": "automatic"}),
        ("post", f"/sources/{source.id}/agent-upgrade/resume", None),
        ("get", f"/sources/{source.id}/agent-upgrades", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        assert getattr(viewer, method)(url, **kwargs).status_code == 403, url
    assert registry.sent == []

    manager_user = _user(session, capabilities=[GlobalCapability.create_source])
    _grant(session, manager_user, source, [Capability.view, Capability.manage_rules])
    manager = _client(session, manager_user)
    assert manager.post(f"/sources/{source.id}/agent-upgrade").status_code == 200
    assert manager.get("/agent-upgrades/agents").status_code == 200
    # The system-wide policy is a System setting: managing sources isn't enough.
    body = {"policy": "automatic"}
    assert manager.patch("/agent-upgrades/settings", json=body).status_code == 403
    settings_admin = _client(
        session, _user(session, capabilities=[GlobalCapability.manage_system_settings])
    )
    assert settings_admin.patch("/agent-upgrades/settings", json=body).status_code == 200


def _grant(session, user, source, capabilities) -> None:
    session.add(
        RoleGrant(
            role_id=user.role_id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=capabilities,
        )
    )
    session.commit()


def test_changing_an_agent_needs_manage_rules_on_its_source(session, registry):
    """Upgrading an agent, setting its policy or resuming it changes the
    source, so create_source alone isn't enough: like editing the source, it
    also needs manage rules on it. Seeing the source isn't enough either."""
    _policy(session)
    source = _agent(session, failures=3)
    registry.connected.add(source.id)
    user = _user(session, capabilities=[GlobalCapability.create_source])
    _grant(session, user, source, [Capability.view])
    manager = _client(session, user)

    for method, url, body in (
        ("post", f"/sources/{source.id}/agent-upgrade", None),
        ("put", f"/sources/{source.id}/agent-upgrade-policy", {"policy": "off"}),
        ("post", f"/sources/{source.id}/agent-upgrade/resume", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        response = getattr(manager, method)(url, **kwargs)
        assert response.status_code == 403, url
        assert response.json()["detail"] == MANAGE_EXISTING_SOURCE_REFUSED
    assert registry.sent == []
    session.refresh(source)
    assert (source.agent_upgrade_policy, source.agent_upgrade_failures) == ("inherit", 3)

    [grant] = session.exec(select(RoleGrant).where(RoleGrant.role_id == user.role_id)).all()
    grant.capabilities = [Capability.view, Capability.manage_rules]
    session.add(grant)
    session.commit()
    resumed = manager.post(f"/sources/{source.id}/agent-upgrade/resume")
    assert resumed.status_code == 200, resumed.text
    assert manager.post(f"/sources/{source.id}/agent-upgrade").status_code == 200
    put = manager.put(f"/sources/{source.id}/agent-upgrade-policy", json={"policy": "off"})
    assert put.status_code == 200


def test_upgrade_all_leaves_out_agents_it_cannot_manage(session, registry):
    """Upgrade all, without picked sources, asks only the agents whose
    source the user may change; picked sources it can't change are skipped
    without their name."""
    _policy(session)
    mine = _agent(session, name="mine")
    other = _agent(session, name="other")
    registry.connected.update({mine.id, other.id})
    user = _user(session, capabilities=[GlobalCapability.create_source])
    _grant(session, user, mine, [Capability.view, Capability.manage_rules])
    manager = _client(session, user)

    every = manager.post("/agent-upgrades", json={})
    assert every.status_code == 200, every.text
    assert [r["source_id"] for r in every.json()] == [mine.id]
    assert {s for s, _c, _f in registry.sent} == {mine.id}

    registry.sent.clear()
    picked = manager.post("/agent-upgrades", json={"source_ids": [other.id]})
    assert picked.json() == [
        {
            "source_id": other.id,
            "name": None,
            "result": "skipped",
            "detail": MANAGE_EXISTING_SOURCE_REFUSED,
        }
    ]
    assert registry.sent == []


def test_reading_upgrades_shows_only_agents_it_can_manage(session, admin, registry):
    """The agents list and an agent's upgrade history show only sources the
    user has manage rules on; another source's history is not found. A super
    admin sees them all."""
    _policy(session)
    mine = _agent(session, name="mine")
    seen = _agent(session, name="seen")
    other = _agent(session, name="other")
    registry.connected.update({mine.id, seen.id, other.id})
    for source in (mine, seen, other):
        assert admin.post(f"/sources/{source.id}/agent-upgrade").status_code == 200
    user = _user(session, capabilities=[GlobalCapability.create_source])
    _grant(session, user, mine, [Capability.view, Capability.manage_rules])
    _grant(session, user, seen, [Capability.view])
    manager = _client(session, user)

    rows = manager.get("/agent-upgrades/agents")
    assert rows.status_code == 200, rows.text
    assert [r["source_id"] for r in rows.json()] == [mine.id]

    history = manager.get(f"/sources/{mine.id}/agent-upgrades")
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 1
    for source in (seen, other):
        response = manager.get(f"/sources/{source.id}/agent-upgrades")
        assert response.status_code == 404, source.name
        assert response.json()["detail"] == "Source not found"

    assert {r["source_id"] for r in admin.get("/agent-upgrades/agents").json()} == {
        mine.id,
        seen.id,
        other.id,
    }
    assert admin.get(f"/sources/{other.id}/agent-upgrades").json()["total"] == 1


# --- upgrading all outdated agents ------------------------------------------------


def test_upgrade_all_asks_the_outdated_agents_and_skips_the_rest(session, admin, registry):
    _policy(session)
    outdated = _agent(session, name="outdated")
    current = _agent(session, version=APP_VERSION, name="current")
    offline = _agent(session, name="offline")
    old = _agent(session, name="old")
    off = _agent(session, name="off", policy="off")
    suspended = _agent(session, name="suspended", failures=3)
    for source in (outdated, current, old, off, suspended):
        registry.connected.add(source.id)
    registry.answers[old.id] = AgentCommandError("unknown", message_type="unknown_error")

    response = admin.post("/agent-upgrades", json={})
    assert response.status_code == 200, response.text
    results = {r["source_id"]: r for r in response.json()}
    assert current.id not in results  # up to date: not part of it
    assert results[outdated.id]["result"] == "accepted"
    assert results[offline.id]["result"] == "skipped"
    assert "isn't connected" in results[offline.id]["detail"]
    assert results[old.id]["result"] == "too_old"
    assert results[off.id]["result"] == "skipped"
    assert results[off.id]["detail"] == POLICY_OFF
    assert results[suspended.id]["result"] == "skipped"
    assert "suspended" in results[suspended.id]["detail"]
    assert {s for s, _c, _f in registry.sent} == {outdated.id, old.id}
    assert {a.target_id for a in _audits(session)} == {outdated.id, old.id}


def test_upgrade_all_with_picked_sources(session, admin, registry):
    _policy(session)
    picked = _agent(session)
    other = _agent(session)
    registry.connected.update({picked.id, other.id})
    response = admin.post("/agent-upgrades", json={"source_ids": [picked.id, 424242]})
    results = {r["source_id"]: r for r in response.json()}
    assert results[picked.id]["result"] == "accepted"
    assert results[424242]["result"] == "skipped"
    assert {s for s, _c, _f in registry.sent} == {picked.id}


def test_upgrade_all_with_the_policy_off_upgrades_nothing(session, admin, registry):
    source = _agent(session)
    registry.connected.add(source.id)
    [result] = admin.post("/agent-upgrades", json={}).json()
    assert result["result"] == "skipped"
    assert registry.sent == []


# --- settings ------------------------------------------------------------------------


def test_the_overview_shows_the_defaults(session, admin, registry, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "agent_binaries_dir", "")
    body = admin.get("/agent-upgrades").json()
    assert body["policy"] == "off"
    assert body["window_days"] == [0, 1, 2, 3, 4, 5, 6]
    assert body["window_start"] == "02:00"
    assert body["window_hours"] == 4
    assert body["failure_limit"] == 3
    assert body["concurrency"] == 5
    assert body["timezone"]
    assert body["next_window_start"] is not None
    assert body["last_window"] is None
    assert body["version"] == APP_VERSION
    assert body["bundled"] is False
    assert body["bundled_reason"]


def test_the_settings_are_saved_and_audited(session, admin):
    response = admin.patch(
        "/agent-upgrades/settings",
        json={
            "policy": "automatic",
            "window_days": [5, 4, 4],
            "window_start": "22:30",
            "window_hours": 8,
            "failure_limit": 5,
            "concurrency": 10,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["policy"] == "automatic"
    assert body["window_days"] == [4, 5]
    assert body["window_start"] == "22:30"
    assert (body["window_hours"], body["failure_limit"], body["concurrency"]) == (8, 5, 10)
    [audit] = _audits(session, "system_settings.update")
    assert audit.event_metadata["agent_upgrade_policy"] == "automatic"
    assert audit.event_metadata["agent_upgrade_window_days"] == [4, 5]


@pytest.mark.parametrize(
    "body",
    [
        {"policy": "sometimes"},
        {"window_days": []},
        {"window_days": [7]},
        {"window_start": "24:00"},
        {"window_start": "2am"},
        {"window_hours": 0},
        {"window_hours": 25},
        {"failure_limit": 0},
        {"failure_limit": 11},
        {"concurrency": 0},
        {"concurrency": 51},
    ],
)
def test_bad_settings_are_refused(session, admin, body):
    assert admin.patch("/agent-upgrades/settings", json=body).status_code == 422
    assert _audits(session, "system_settings.update") == []


# --- the agents table ------------------------------------------------------------------


def test_the_agents_table(session, admin, registry):
    _policy(session, "automatic")
    connected = _agent(session, name="a-connected")
    _agent(session, name="b-off", policy="off")
    suspended = _agent(session, name="c-suspended", failures=3)
    _agent(session, name="ssh", protocol=Protocol.ssh)
    registry.connected.update({connected.id, suspended.id})
    admin.post(f"/sources/{connected.id}/agent-upgrade")

    rows = admin.get("/agent-upgrades/agents").json()
    assert [r["name"] for r in rows] == ["a-connected", "b-off", "c-suspended"]
    first, second, third = rows
    assert first["connected"] is True
    assert first["outdated"] is True
    assert (first["policy"], first["effective_policy"]) == ("inherit", "automatic")
    assert first["upgrading"] is True
    assert first["last_run"]["status"] == "accepted"
    assert "already in progress" in first["blocker"]
    assert (second["effective_policy"], second["blocker"]) == ("off", POLICY_OFF)
    assert second["last_run"] is None
    assert (third["suspended"], third["failures"]) == (True, 3)
    assert third["blocker"] is None  # a manual upgrade is still possible


def test_a_sources_policy_is_set_and_audited(session, admin, registry):
    source = _agent(session)
    response = admin.put(f"/sources/{source.id}/agent-upgrade-policy", json={"policy": "manual"})
    assert response.status_code == 200
    assert (response.json()["policy"], response.json()["effective_policy"]) == (
        "manual",
        "manual",
    )
    [audit] = _audits(session, "source.update")
    assert audit.event_metadata["changes"] == {
        "agent_upgrade_policy": {"from": "inherit", "to": "manual"}
    }
    bad = admin.put(f"/sources/{source.id}/agent-upgrade-policy", json={"policy": "always"})
    assert bad.status_code == 422
    ssh = _agent(session, protocol=Protocol.ssh)
    put = admin.put(f"/sources/{ssh.id}/agent-upgrade-policy", json={"policy": "manual"})
    assert put.status_code == 409


def test_resume_clears_the_failures_and_is_audited(session, admin, registry):
    source = _agent(session, failures=3)
    row = admin.get("/agent-upgrades/agents").json()[0]
    assert row["suspended"] is True
    response = admin.post(f"/sources/{source.id}/agent-upgrade/resume")
    assert response.status_code == 200
    assert (response.json()["suspended"], response.json()["failures"]) == (False, 0)
    [audit] = _audits(session, "agent.upgrade_resume")
    assert audit.target_id == source.id
    assert audit.event_metadata["failures"] == 3


def test_the_history_is_paged_most_recent_first(session, admin, admin_user, registry):
    _policy(session)
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = AgentTimeoutError("no answer")
    for _ in range(3):
        admin.post(f"/sources/{source.id}/agent-upgrade")
    page = admin.get(f"/sources/{source.id}/agent-upgrades?limit=2").json()
    assert page["total"] == 3
    ids = [r["id"] for r in page["items"]]
    assert ids == sorted(ids, reverse=True)
    assert page["items"][0]["requested_by"] == admin_user.username
    rest = admin.get(f"/sources/{source.id}/agent-upgrades?offset=2&limit=2").json()
    assert len(rest["items"]) == 1
    assert rest["items"][0]["id"] < ids[-1]
    assert admin.get(f"/sources/{source.id}/agent-upgrades?limit=500").status_code == 422


def test_the_progress_reaches_the_history(session, admin, registry):
    _policy(session)
    source = _agent(session)
    registry.connected.add(source.id)
    admin.post(f"/sources/{source.id}/agent-upgrade")
    [(_s, _c, fields)] = registry.sent
    handle_message(
        session,
        source.id,
        {"type": "upgrade_progress", "id": fields["command_id"], "stage": "restarting"},
    )
    session.commit()
    [run] = admin.get(f"/sources/{source.id}/agent-upgrades").json()["items"]
    assert run["status"] == "restarting"


def test_deleting_a_source_deletes_its_attempts(session, admin, registry):
    _policy(session)
    source = _agent(session)
    registry.connected.add(source.id)
    admin.post(f"/sources/{source.id}/agent-upgrade")
    assert admin.delete(f"/sources/{source.id}").status_code == 204
    assert session.exec(select(AgentUpgradeRun)).all() == []


def test_the_old_setting_is_gone_from_system_settings(session):
    from app.api.system_settings import router as settings_router

    app = FastAPI()
    app.include_router(settings_router)
    app.dependency_overrides[get_session] = lambda: session
    user = _user(session, super_admin=True)
    app.dependency_overrides[get_current_active_user] = lambda: user
    client = TestClient(app)
    assert "agent_remote_upgrade_enabled" not in client.get("/system-settings").json()
