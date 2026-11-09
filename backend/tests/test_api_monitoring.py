import uuid
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
    assert body["scratch"] == {
        "used_bytes": 0,
        "max_bytes": 1024,
        "used_fraction": 0.0,
        "min_free_bytes": 0,
    }
    assert body["sources_by_protocol"]["local"] == 1
    assert body["sources_by_protocol"]["ssh"] == 1
    assert body["agent"] == {"configured": 0, "connected": 0}
    assert body["scheduler"]["running"] is True
    assert body["alerts"]["pending_deliveries"] == 0
    assert body["alerts"]["failed_deliveries"] == 0


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


def test_health_reports_logs_and_size_limit_pressure(admin_client, tmp_path, monkeypatch):
    from app import log_rotation
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "log_dir", str(tmp_path))
    (tmp_path / "perchtail.log").write_bytes(b"x" * 10)
    (tmp_path / "perchtail.log.2026-11-01.1.gz").write_bytes(b"x" * 5)
    (tmp_path / "other.txt").write_bytes(b"x" * 100)
    token = admin_client.post("/monitoring/token").json()["token"]

    def logs():
        return admin_client.get(
            "/monitoring/health", headers={"Authorization": f"Bearer {token}"}
        ).json()["logs"]

    log_rotation.reset_retention_pressure()
    assert logs()["used_bytes"] == 15
    assert logs()["size_limit_deleted_early"] is False

    monkeypatch.setattr(
        log_rotation,
        "_pressure",
        log_rotation.RetentionPressure(deleted_files=3, newest_deleted_day="2026-10-30"),
    )
    body = logs()
    assert body["size_limit_deleted_early"] is True
    assert body["size_limit_newest_deleted_day"] == "2026-10-30"


def test_health_shows_each_jobs_last_run(session, admin_client):
    """#327: a job that fails, or never ran, shows in the health report."""
    from app.models import JobRun

    session.add(
        JobRun(
            job_id="job",
            last_started_at=datetime(2026, 1, 1, 12, 0),
            last_finished_at=datetime(2026, 1, 1, 12, 1),
            last_ok=False,
            last_error="RuntimeError: boom",
            last_duration_ms=60000,
        )
    )
    session.commit()

    token = admin_client.post("/monitoring/token").json()["token"]
    body = admin_client.get(
        "/monitoring/health", headers={"Authorization": f"Bearer {token}"}
    ).json()
    (job,) = body["scheduler"]["jobs"]
    assert job["id"] == "job"
    assert job["last_ok"] is False
    assert job["last_error"] == "RuntimeError: boom"
    assert job["last_finished_at"] == "2026-01-01T12:01:00"
    assert job["last_success_at"] is None


# --- #330: the fuller health report, #331: /metrics -------------------------


class _Usage:
    def __init__(self, free, total):
        self.free = free
        self.total = total
        self.used = total - free


def _health(client):
    token = client.post("/monitoring/token").json()["token"]
    return client.get("/monitoring/health", headers={"Authorization": f"Bearer {token}"}).json()


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch):
    monkeypatch.setattr("app.api.monitoring.disk_usage", lambda path: _Usage(900, 1000))


def test_health_reports_disks_instance_and_the_new_sections(session, admin_client):
    body = _health(admin_client)

    assert body["status"] == "ok"
    assert set(body["disks"]) == {"data", "scratch", "logs"}
    assert body["disks"]["data"] == {"free_bytes": 900, "total_bytes": 1000, "free_fraction": 0.9}
    assert body["instance"]["pid"] > 0
    assert body["instance"]["hostname"]
    assert set(body["database"]) >= {"reachable", "latency_ms", "size_bytes", "wal_bytes"}
    assert body["name_index"]["sources"] == 0
    assert body["name_index"]["oldest_age_seconds"] is None
    assert body["source_checks"] == {"failing": 0, "backing_off": 0}
    assert body["audit_integrity"] == {
        "status": "unknown",
        "last_checked_at": None,
        "broken_row_id": None,
    }
    assert body["scheduler"]["failed_jobs"] == 0
    assert "last_pass_age_seconds" in body["alerts"]


def test_health_reports_an_unreadable_directory_as_unknown(admin_client, monkeypatch):
    def missing(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr("app.api.monitoring.disk_usage", missing)
    body = _health(admin_client)
    assert body["disks"]["logs"] == {"free_bytes": None, "total_bytes": None, "free_fraction": None}
    assert body["status"] == "ok"


def test_health_reports_degraded_when_a_disk_is_nearly_full(admin_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "scratch_dir", "/somewhere/scratch")
    monkeypatch.setattr(
        "app.api.monitoring.disk_usage",
        lambda path: _Usage(10, 1000) if "scratch" in str(path) else _Usage(900, 1000),
    )
    body = _health(admin_client)
    assert body["disks"]["scratch"]["free_fraction"] == 0.01
    assert body["status"] == "degraded"


def test_health_reports_degraded_when_a_jobs_last_run_failed(session, admin_client):
    from app.models import JobRun

    session.add(JobRun(job_id="job", last_ok=False, last_error="RuntimeError: boom"))
    session.commit()

    body = _health(admin_client)
    assert body["scheduler"]["failed_jobs"] == 1
    assert body["status"] == "degraded"


def test_a_job_that_works_again_is_no_longer_counted(session, admin_client):
    from app.models import JobRun

    success = datetime(2026, 1, 1, 12, 0)
    session.add(JobRun(job_id="job", last_ok=True, last_success_at=success))
    session.commit()

    body = _health(admin_client)
    assert body["scheduler"]["failed_jobs"] == 0
    assert body["scheduler"]["jobs"][0]["last_success_age_seconds"] > 0
    assert body["status"] == "ok"


def test_health_reports_degraded_when_the_audit_chain_is_broken(session, admin_client):
    from app.auth.models import AuditChainState

    session.add(
        AuditChainState(status="broken", broken_row_id=42, last_checked_at=datetime(2026, 1, 1))
    )
    session.commit()

    body = _health(admin_client)
    assert body["audit_integrity"] == {
        "status": "broken",
        "last_checked_at": "2026-01-01T00:00:00",
        "broken_row_id": 42,
    }
    assert body["status"] == "degraded"


def test_race_breaks_waiting_to_be_recorded_do_not_degrade(session, admin_client):
    from app.auth.models import AuditChainState

    session.add(AuditChainState(status="race_breaks", broken_row_id=7))
    session.commit()
    assert _health(admin_client)["status"] == "ok"


def test_health_reports_the_name_index_and_source_checks(session, admin_client):
    from datetime import timedelta

    from app.timeutils import utcnow

    now = utcnow()

    def source(name, **values):
        session.add(Source(name=name, protocol=Protocol.ssh, host=name, base_path="/", **values))

    source("fresh", name_indexed_at=now - timedelta(minutes=1))
    source(
        "old",
        name_indexed_at=now - timedelta(hours=2),
        name_index_message="3 folders could not be listed",
        status="failing",
    )
    source("never", check_backoff_until=now + timedelta(minutes=10), check_failures=3)
    source("opted-out", name_index_enabled=False, name_indexed_at=now - timedelta(days=9))
    source("disabled", enabled=False, status="failing", name_indexed_at=now - timedelta(days=9))
    session.commit()

    body = _health(admin_client)
    index = body["name_index"]
    assert index["enabled"] is True
    assert index["sources"] == 3
    assert index["never_indexed"] == 1
    assert index["sources_with_message"] == 1
    assert 7000 < index["oldest_age_seconds"] < 7300
    assert body["source_checks"] == {"failing": 1, "backing_off": 1}
    # Failing sources are about the servers, not PerchTail: still ok.
    assert body["status"] == "ok"


def test_health_reports_the_sqlite_file_and_wal_sizes(admin_client, monkeypatch, tmp_path):
    db_file = tmp_path / "perchtail.db"
    db_file.write_bytes(b"x" * 4096)
    monkeypatch.setattr("app.api.monitoring.sqlite_file", lambda: db_file)

    assert _health(admin_client)["database"]["wal_bytes"] == 0
    (tmp_path / "perchtail.db-wal").write_bytes(b"x" * 100)
    database = _health(admin_client)["database"]
    assert database["size_bytes"] == 4096
    assert database["wal_bytes"] == 100


@pytest.fixture()
def metrics_client(session):
    from app.api.monitoring import metrics_router

    user = _make_user(session, is_super_admin=True)
    app = FastAPI()
    app.include_router(monitoring_router)
    app.include_router(metrics_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _samples(text):
    samples = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            samples[name] = float(value)
    return samples


def test_metrics_needs_the_monitoring_token(metrics_client):
    assert metrics_client.get("/metrics").status_code == 401
    response = metrics_client.get("/metrics", headers={"Authorization": "Bearer whatever"})
    assert response.status_code == 503
    metrics_client.post("/monitoring/token")
    response = metrics_client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_metrics_are_the_health_report_in_prometheus_format(session, metrics_client):
    from app.models import JobRun
    from app.version import APP_VERSION

    session.add(
        JobRun(
            job_id="job",
            last_ok=True,
            last_success_at=datetime(2026, 1, 1),
            last_duration_ms=1500,
        )
    )
    session.add(Source(name="s1", protocol=Protocol.local, host="local", base_path="/"))
    session.commit()
    token = metrics_client.post("/monitoring/token").json()["token"]

    response = metrics_client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = response.text
    assert "# TYPE perchtail_status gauge" in text
    samples = _samples(text)
    assert samples["perchtail_status"] == 0
    assert samples[f'perchtail_build_info{{version="{APP_VERSION}"}}'] == 1
    assert samples["perchtail_database_reachable"] == 1
    assert samples['perchtail_disk_free_bytes{dir="data"}'] == 900
    assert samples['perchtail_disk_total_bytes{dir="logs"}'] == 1000
    assert samples['perchtail_sources_enabled{protocol="local"}'] == 1
    assert samples['perchtail_job_last_ok{job="job"}'] == 1
    assert samples['perchtail_job_last_success_timestamp_seconds{job="job"}'] == 1767225600
    assert samples['perchtail_job_last_duration_seconds{job="job"}'] == 1.5
    assert samples["perchtail_audit_integrity_ok"] == 0
    assert samples["perchtail_scheduler_running"] == 1
    # Unknown values are left out rather than written as NaN.
    assert "perchtail_name_index_oldest_age_seconds" not in text


def test_metrics_status_follows_the_health_status(session, metrics_client):
    from app.models import JobRun

    session.add(JobRun(job_id="job", last_ok=False))
    session.commit()
    token = metrics_client.post("/monitoring/token").json()["token"]
    text = metrics_client.get("/metrics", headers={"Authorization": f"Bearer {token}"}).text
    samples = _samples(text)
    assert samples["perchtail_status"] == 1
    assert samples["perchtail_jobs_failed"] == 1
    assert samples['perchtail_job_last_ok{job="job"}'] == 0


def test_metric_labels_are_escaped():
    from app.metrics import _escape

    assert _escape('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def test_health_and_metrics_report_the_limits_in_use(session, metrics_client):
    from app.limits import current_limits, get_limiter

    held = get_limiter().connector(current_limits(session), 42)
    transfer = get_limiter().transfer(current_limits(session), 7)
    body = _health(metrics_client)
    assert body["limits"] == {
        "global_in_use": 1,
        "global_max": 16,
        "per_source_max": 4,
        "busiest_source_in_use": 1,
        "sources_in_use": {"42": 1},
        "per_user_transfers_max": 2,
        "transfers_in_use": 1,
        "busiest_user_transfers": 1,
        "rejected": {"global": 0, "source": 0, "user": 0},
    }
    token = metrics_client.post("/monitoring/token").json()["token"]
    text = metrics_client.get("/metrics", headers={"Authorization": f"Bearer {token}"}).text
    samples = _samples(text)
    assert samples["perchtail_connector_slots_in_use"] == 1
    assert samples["perchtail_connector_slots_max"] == 16
    assert samples["perchtail_transfers_in_use"] == 1
    assert samples['perchtail_limit_rejections_total{limit="user"}'] == 0
    assert "# TYPE perchtail_limit_rejections_total counter" in text
    held.release()
    transfer.release()
