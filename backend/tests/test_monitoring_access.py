"""Who may call the monitoring endpoints (#366, app/monitoring_access.py)."""

import pytest
from app.config import Settings, get_settings
from app.db import get_session
from app.main import app
from app.models import MonitoringToken
from app.monitoring_access import hash_token
from app.timeutils import utcnow
from fastapi.testclient import TestClient

LOCAL = ("127.0.0.1", 50000)
LOCAL_V6 = ("::1", 50000)
REMOTE = ("203.0.113.7", 50000)
TOKEN = "monitoring-secret"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def probes(session, monkeypatch):
    """The probes on the real app, with the monitoring token set."""
    session.add(MonitoringToken(token_hash=hash_token(TOKEN), created_at=utcnow()))
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    # /readyz's own checks aren't what's tested here.
    monkeypatch.setattr("app.main.readiness_failures", lambda *args: [])
    yield lambda address: TestClient(app, client=address)
    app.dependency_overrides.pop(get_session, None)


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_a_local_caller_without_proxy_headers_needs_no_token(probes, path):
    assert probes(LOCAL).get(path).status_code == 200
    assert probes(LOCAL_V6).get(path).status_code == 200


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP"])
@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_a_local_caller_through_a_proxy_is_not_local(probes, path, header):
    # A reverse proxy on the same host connects from loopback too.
    response = probes(LOCAL).get(path, headers={header: "for=198.51.100.9"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_a_remote_caller_needs_the_token(probes, path):
    response = probes(REMOTE).get(path)
    assert response.status_code == 401
    # Nothing about the instance's health.
    assert response.json() == {"detail": "Unauthorized"}
    assert probes(REMOTE).get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert probes(REMOTE).get(path, headers=BEARER).status_code == 200


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_a_remote_caller_in_an_allowed_network_needs_no_token(probes, monkeypatch, path):
    monkeypatch.setattr(get_settings(), "probe_allowed_networks", "10.0.0.0/8, 203.0.113.0/24")
    assert probes(REMOTE).get(path).status_code == 200
    assert probes(("198.51.100.9", 1)).get(path).status_code == 401


def test_without_a_monitoring_token_only_local_and_allowed_callers_pass(session, monkeypatch):
    app.dependency_overrides[get_session] = lambda: session
    try:
        assert TestClient(app, client=LOCAL).get("/healthz").status_code == 200
        response = TestClient(app, client=REMOTE).get("/healthz", headers=BEARER)
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_an_unknown_caller_address_is_not_local(probes):
    # Starlette's TestClient default ("testclient") isn't an address at all.
    assert probes(("testclient", 50000)).get("/healthz").status_code == 401


@pytest.fixture()
def monitoring(session):
    """/monitoring/health and /metrics on the real app, token set."""
    session.add(MonitoringToken(token_hash=hash_token(TOKEN), created_at=utcnow()))
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    yield lambda address: TestClient(app, client=address)
    app.dependency_overrides.pop(get_session, None)


@pytest.mark.parametrize("path", ["/monitoring/health", "/metrics"])
def test_allowed_networks_refuse_a_valid_token_from_elsewhere(monitoring, monkeypatch, path):
    # Without the setting, the token alone decides: no token, no answer.
    assert monitoring(REMOTE).get(path).status_code == 401
    monkeypatch.setattr(get_settings(), "monitoring_allowed_networks", "192.0.2.0/24")
    response = monitoring(REMOTE).get(path, headers=BEARER)
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    # From the allowed network the token is still needed.
    assert monitoring(("192.0.2.10", 1)).get(path).status_code == 401
    assert monitoring(("192.0.2.10", 1)).get(path, headers=BEARER).status_code != 403


def test_a_malformed_network_stops_startup():
    with pytest.raises(ValueError, match="PROBE_ALLOWED_NETWORKS"):
        Settings(probe_allowed_networks="10.0.0.0/8, not-a-network")
    with pytest.raises(ValueError, match="MONITORING_ALLOWED_NETWORKS"):
        Settings(monitoring_allowed_networks="300.1.2.3")
