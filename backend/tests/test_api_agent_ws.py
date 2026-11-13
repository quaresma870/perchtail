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


def test_reconnecting_agent_stays_connected_when_the_old_socket_closes(session):
    """Regression: the old socket's cleanup used to unregister the source
    unconditionally, removing the connection that had just replaced it."""
    source = _agent_source(session, "s3cret")
    registry = get_agent_registry()
    client = _client(session)
    headers = {"authorization": "Bearer s3cret"}

    old = client.websocket_connect("/agent/connect", headers=headers)
    old.__enter__()
    assert _wait_until(lambda: registry.is_connected(source.id))
    with client.websocket_connect("/agent/connect", headers=headers):
        old.__exit__(None, None, None)
        # Give the old connection's handler time to run its cleanup.
        time.sleep(0.2)
        assert registry.is_connected(source.id)
    assert _wait_until(lambda: not registry.is_connected(source.id))


def test_connect_records_the_os_the_agent_reports(session):
    """#189: rules for a Windows agent are matched case-insensitively, so
    the backend needs to know which OS the agent runs on."""
    source = _agent_source(session, "s3cret")
    registry = get_agent_registry()
    client = _client(session)
    headers = {"authorization": "Bearer s3cret", "x-perchtail-agent-os": "Windows"}
    with client.websocket_connect("/agent/connect", headers=headers):
        assert _wait_until(lambda: registry.is_connected(source.id))
        session.refresh(source)
        assert source.agent_os == "windows"
    assert _wait_until(lambda: not registry.is_connected(source.id))

    # An older agent (no header) or a malformed value keeps what's known.
    for extra in ({}, {"x-perchtail-agent-os": "win dows; drop"}):
        with client.websocket_connect(
            "/agent/connect", headers={"authorization": "Bearer s3cret", **extra}
        ):
            assert _wait_until(lambda: registry.is_connected(source.id))
        assert _wait_until(lambda: not registry.is_connected(source.id))
        session.refresh(source)
        assert source.agent_os == "windows"


def test_a_streamed_fetch_through_the_endpoint_lands_in_the_destination(session, tmp_path):
    """#205 end to end: the backend asks for chunks, the agent's
    fetch_chunk messages go through the WebSocket endpoint into the file."""
    import asyncio
    import base64
    import threading

    from app.collectors import agent as agent_collector

    source = _agent_source(session, "s3cret")
    registry = get_agent_registry()
    previous_loop = registry._loop
    content = b"first chunk|" + b"second chunk"
    destination = tmp_path / "fetched"
    errors: list[Exception] = []

    def fetch():
        try:
            agent_collector.fetch_file(source, "app.log", destination)
        except Exception as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    try:
        with _client(session) as client:
            registry.bind_loop(client.portal.call(asyncio.get_running_loop))
            with client.websocket_connect(
                "/agent/connect", headers={"authorization": "Bearer s3cret"}
            ) as ws:
                assert _wait_until(lambda: registry.is_connected(source.id))
                thread = threading.Thread(target=fetch)
                thread.start()

                command = ws.receive_json()
                assert command["type"] == "fetch" and command["path"] == "app.log"
                assert command["chunk_size"] == agent_collector.FETCH_CHUNK_BYTES
                for seq, part in enumerate([content[:12], content[12:]]):
                    ws.send_json(
                        {
                            "type": "fetch_chunk",
                            "id": command["id"],
                            "seq": seq,
                            "data_b64": base64.b64encode(part).decode(),
                        }
                    )
                ws.send_json(
                    {
                        "type": "fetch_result",
                        "id": command["id"],
                        "streamed": True,
                        "chunks": 2,
                        "size": len(content),
                    }
                )
                thread.join(timeout=5)
    finally:
        registry.bind_loop(previous_loop)

    assert errors == []
    assert destination.read_bytes() == content


def test_connect_records_the_version_the_agent_reports(session):
    """#215: shown on the source page, with a warning when it's older than
    the server."""
    source = _agent_source(session, "s3cret")
    registry = get_agent_registry()
    client = _client(session)
    headers = {"authorization": "Bearer s3cret", "x-perchtail-agent-version": "0.6.0"}
    with client.websocket_connect("/agent/connect", headers=headers):
        assert _wait_until(lambda: registry.is_connected(source.id))
        session.refresh(source)
        assert source.agent_version == "0.6.0"
    assert _wait_until(lambda: not registry.is_connected(source.id))

    # An agent older than 0.6 (no header), or a malformed value, clears it:
    # the source mustn't keep showing the version of the agent it replaced.
    for extra in ({}, {"x-perchtail-agent-version": "0.6.0; <script>"}):
        with client.websocket_connect(
            "/agent/connect", headers={"authorization": "Bearer s3cret", **extra}
        ):
            assert _wait_until(lambda: registry.is_connected(source.id))
            session.refresh(source)
            assert source.agent_version is None
        assert _wait_until(lambda: not registry.is_connected(source.id))


# --- the server closes the connection when the agent may no longer use it ---


def _admin_client(session):
    import uuid

    from app.api.auth import get_current_active_user
    from app.api.sources import router as sources_router
    from app.auth.models import Role, User

    role = Role(name=f"admin-{uuid.uuid4().hex}", is_super_admin=True)
    session.add(role)
    session.commit()
    admin = User(username=f"admin-{uuid.uuid4().hex[:8]}", role_id=role.id)
    session.add(admin)
    session.commit()
    session.refresh(admin)
    app = FastAPI()
    app.include_router(agent_ws_router)
    app.include_router(sources_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: admin
    return TestClient(app)


def _connected_then(session, change, *, expect_closed=True):
    """Connects an agent, runs `change(client, source_id)` and returns
    whether the server closed the agent's socket, and how."""
    import asyncio

    source = _agent_source(session, "s3cret")
    source_id = source.id
    registry = get_agent_registry()
    previous_loop = registry._loop
    try:
        with _admin_client(session) as client:
            registry.bind_loop(client.portal.call(asyncio.get_running_loop))
            with client.websocket_connect(
                "/agent/connect", headers={"authorization": "Bearer s3cret"}
            ) as ws:
                assert _wait_until(lambda: registry.is_connected(source_id))
                change(client, source_id)
                if not expect_closed:
                    assert registry.is_connected(source_id)
                    return None
                assert not registry.is_connected(source_id)
                try:
                    ws.receive_json()
                except WebSocketDisconnect as exc:
                    return exc.code
                return None
    finally:
        registry.bind_loop(previous_loop)


def test_regenerating_the_token_closes_the_agent_connected_with_the_old_one(session):
    def regenerate(client, source_id):
        assert client.post(f"/sources/{source_id}/agent-token").status_code == 200

    assert _connected_then(session, regenerate) == 1008


def test_disabling_the_source_closes_its_agent_connection(session):
    def disable(client, source_id):
        response = client.patch(f"/sources/{source_id}", json={"enabled": False})
        assert response.status_code == 200

    assert _connected_then(session, disable) == 1008


def test_deleting_the_source_closes_its_agent_connection(session):
    def delete(client, source_id):
        assert client.delete(f"/sources/{source_id}").status_code == 204

    assert _connected_then(session, delete) == 1008


def test_other_changes_keep_the_agent_connected(session):
    def rename(client, source_id):
        response = client.patch(f"/sources/{source_id}", json={"name": "agent01-renamed"})
        assert response.status_code == 200

    _connected_then(session, rename, expect_closed=False)


def test_the_close_waits_for_the_commit(session):
    from app.agent_registry import disconnect_after_commit

    source = _agent_source(session, "s3cret")
    registry = get_agent_registry()
    closed = []
    original = registry.disconnect
    registry.disconnect = lambda sid, reason: closed.append((sid, reason)) or True
    try:
        disconnect_after_commit(session, source.id, "source disabled")
        assert closed == []
        session.rollback()
        session.commit()
        assert closed == [(source.id, "source disabled")]
    finally:
        registry.disconnect = original


def test_a_token_regenerated_during_the_handshake_still_closes_the_connection(session, monkeypatch):
    # The admin's change lands after the token was checked but before the
    # socket registered, so its disconnect found nothing to close; the
    # check after registering has to catch it.
    from sqlalchemy import update

    source = _agent_source(session, "s3cret")
    source_id = source.id
    registry = get_agent_registry()
    original_register = registry.register
    registered = []

    def register_after_a_regeneration(sid, websocket):
        session.execute(
            update(Source).where(Source.id == sid).values(agent_token_hash=_hash_token("new"))
        )
        session.commit()
        registry.disconnect(sid, "agent token regenerated")  # finds nothing yet
        replaced = original_register(sid, websocket)
        registered.append(sid)
        return replaced

    monkeypatch.setattr(registry, "register", register_after_a_regeneration)
    client = _client(session)
    with client.websocket_connect(
        "/agent/connect", headers={"authorization": "Bearer s3cret"}
    ) as ws:
        assert _wait_until(lambda: registered)
        # Without the check it stays registered (and open) for good.
        assert _wait_until(lambda: not registry.is_connected(source_id))
        try:
            ws.receive_json()
            code = None
        except WebSocketDisconnect as exc:
            code = exc.code
    assert code == 1008
