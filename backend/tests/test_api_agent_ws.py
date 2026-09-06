import time

from app.agent_registry import get_agent_registry
from app.api.agent_ws import _hash_token
from app.api.agent_ws import router as agent_ws_router
from app.db import get_session
from app.models import Protocol, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _client(session) -> TestClient:
    app = FastAPI()
    app.include_router(agent_ws_router)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _agent_source(session, token: str, *, enabled: bool = True) -> Source:
    source = Source(
        name="agent01",
        protocol=Protocol.agent,
        host="agent01",
        base_path="/var/log/app",
        agent_token_hash=_hash_token(token),
        enabled=enabled,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_connect_without_token_is_rejected(session):
    _agent_source(session, "s3cret")
    client = _client(session)
    try:
        with client.websocket_connect("/agent/connect"):
            pass
        raised = False
    except WebSocketDisconnect:
        raised = True
    assert raised


def test_connect_with_wrong_token_is_rejected(session):
    _agent_source(session, "s3cret")
    client = _client(session)
    try:
        with client.websocket_connect(
            "/agent/connect", headers={"authorization": "Bearer wrong-token"}
        ):
            pass
        raised = False
    except WebSocketDisconnect:
        raised = True
    assert raised


def test_connect_when_source_disabled_is_rejected(session):
    _agent_source(session, "s3cret", enabled=False)
    client = _client(session)
    try:
        with client.websocket_connect("/agent/connect", headers={"authorization": "Bearer s3cret"}):
            pass
        raised = False
    except WebSocketDisconnect:
        raised = True
    assert raised


def test_valid_token_registers_connection_and_updates_last_seen(session):
    source = _agent_source(session, "s3cret")
    registry = get_agent_registry()
    assert registry.is_connected(source.id) is False

    client = _client(session)
    with client.websocket_connect("/agent/connect", headers={"authorization": "Bearer s3cret"}):
        assert _wait_until(lambda: registry.is_connected(source.id))
        session.refresh(source)
        assert source.agent_last_seen_at is not None

    assert _wait_until(lambda: not registry.is_connected(source.id))
