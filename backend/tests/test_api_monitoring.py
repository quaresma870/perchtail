from datetime import datetime

import pytest
from app.api.auth import get_current_active_user
from app.api.monitoring import router as monitoring_router
from app.auth.models import AuditLog, Role, User
from app.db import get_session
from app.models import MonitoringToken, Protocol, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


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


class _FakeJob:
    def __init__(self, id, next_run_time):
        self.id = id
        self.next_run_time = next_run_time


class _FakeScheduler:
    def __init__(self, running=True, jobs=None):
        self.running = running
        self._jobs = jobs if jobs is not None else [_FakeJob("job", datetime(2030, 1, 1))]

    def get_jobs(self):
        return self._jobs


class _FakeScratchStore:
    def __init__(self, used_bytes=0, max_bytes=1024):
        self._used_bytes = used_bytes
        self.max_bytes = max_bytes

    def total_bytes(self):
        return self._used_bytes


class _FakeAgentRegistry:
    def __init__(self, connected=0):
        self._connected = connected

    def connected_count(self):
        return self._connected


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(monitoring_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


@pytest.fixture()
def admin_client(session, client_for):
    return client_for(_make_user(session, is_super_admin=True))


@pytest.fixture()
def plain_client(session, client_for):
    return client_for(_make_user(session))


@pytest.fixture(autouse=True)
def _healthy_stubs(monkeypatch):
    """Every test that exercises GET /monitoring/health gets a deterministic,
    "everything is fine" stand-in for the process-wide scheduler/scratch/
    agent-registry singletons, so status defaults to "ok" unless a test
    explicitly overrides one of these to check a degraded path."""
    monkeypatch.setattr("app.api.monitoring.scheduler", _FakeScheduler())
    monkeypatch.setattr("app.api.monitoring.get_scratch_store", lambda: _FakeScratchStore())
    monkeypatch.setattr("app.api.monitoring.get_agent_registry", lambda: _FakeAgentRegistry())


def test_regenerate_token_requires_manage_capability(plain_client):
    response = plain_client.post("/monitoring/token")
    assert response.status_code == 403


def test_regenerate_token_returns_plaintext_once_and_hashes_at_rest(session, admin_client):
    response = admin_client.post("/monitoring/token")
    assert response.status_code == 200
    token = response.json()["token"]
    assert token

    row = session.exec(select(MonitoringToken)).one()
    assert row.token_hash != token

    audit = session.exec(
        select(AuditLog).where(AuditLog.action == "monitoring_token.regenerate")
    ).one()
    assert audit.user_id is not None


def test_regenerate_token_replaces_singleton_row_and_invalidates_old_token(session, admin_client):
    first = admin_client.post("/monitoring/token").json()["token"]
    second = admin_client.post("/monitoring/token").json()["token"]
    assert first != second

    rows = session.exec(select(MonitoringToken)).all()
    assert len(rows) == 1

    stale_response = admin_client.get(
        "/monitoring/health", headers={"Authorization": f"Bearer {first}"}
    )
    assert stale_response.status_code == 401

    fresh_response = admin_client.get(
        "/monitoring/health", headers={"Authorization": f"Bearer {second}"}
    )
    assert fresh_response.status_code == 200


def test_token_status_reports_whether_configured(admin_client):
    assert admin_client.get("/monitoring/token").json() == {"configured": False}

    admin_client.post("/monitoring/token")

    assert admin_client.get("/monitoring/token").json() == {"configured": True}


def test_health_requires_bearer_token(admin_client):
    admin_client.post("/monitoring/token")
    response = admin_client.get("/monitoring/health")
    assert response.status_code == 401


def test_health_rejects_invalid_token(admin_client):
    admin_client.post("/monitoring/token")
    response = admin_client.get(
        "/monitoring/health", headers={"Authorization": "Bearer not-the-real-token"}
    )
    assert response.status_code == 401


def test_health_returns_503_when_no_token_configured(admin_client):
    response = admin_client.get("/monitoring/health", headers={"Authorization": "Bearer whatever"})
    assert response.status_code == 503


def test_health_reports_ok_shape_with_valid_token(session, admin_client):
    session.add(
        Source(name="s1", protocol=Protocol.local, host="local", base_path="/", enabled=True)
    )
    session.add(Source(name="s2", protocol=Protocol.ssh, host="h", base_path="/", enabled=True))
    session.add(
        Source(name="s3-disabled", protocol=Protocol.ssh, host="h2", base_path="/", enabled=False)
    )
    session.commit()

    token = admin_client.post("/monitoring/token").json()["token"]
    response = admin_client.get("/monitoring/health", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "ok"
    assert body["database"]["reachable"] is True
    assert body["scratch"] == {"used_bytes": 0, "max_bytes": 1024, "used_fraction": 0.0}
    assert body["sources_by_protocol"]["local"] == 1
    assert body["sources_by_protocol"]["ssh"] == 1
    assert body["agent"] == {"configured": 0, "connected": 0}
    assert body["scheduler"]["running"] is True
    assert body["search_index"]["last_sweep_at"] is None


def test_health_reports_degraded_when_scheduler_not_running(admin_client, monkeypatch):
    monkeypatch.setattr("app.api.monitoring.scheduler", _FakeScheduler(running=False))
    token = admin_client.post("/monitoring/token").json()["token"]

    response = admin_client.get("/monitoring/health", headers={"Authorization": f"Bearer {token}"})
    assert response.json()["status"] == "degraded"


def test_health_reports_degraded_when_scratch_nearly_full(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.api.monitoring.get_scratch_store",
        lambda: _FakeScratchStore(used_bytes=1000, max_bytes=1024),
    )
    token = admin_client.post("/monitoring/token").json()["token"]

    response = admin_client.get("/monitoring/health", headers={"Authorization": f"Bearer {token}"})
    assert response.json()["status"] == "degraded"


def test_health_reports_degraded_when_agent_sources_configured_but_none_connected(
    session, admin_client, monkeypatch
):
    session.add(
        Source(name="agent-src", protocol=Protocol.agent, host="a", base_path="/", enabled=True)
    )
    session.commit()
    monkeypatch.setattr(
        "app.api.monitoring.get_agent_registry", lambda: _FakeAgentRegistry(connected=0)
    )

    token = admin_client.post("/monitoring/token").json()["token"]
    response = admin_client.get("/monitoring/health", headers={"Authorization": f"Bearer {token}"})
    body = response.json()
    assert body["agent"] == {"configured": 1, "connected": 0}
    assert body["status"] == "degraded"
