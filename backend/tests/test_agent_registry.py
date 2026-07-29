import asyncio
import threading
import time

import pytest
from app.agent_registry import (
    AgentCommandError,
    AgentNotConnectedError,
    AgentRegistry,
    AgentTimeoutError,
)


class FakeWebSocket:
    """Records what get sent instead of touching a real socket — the
    registry only ever calls `send_json` on it."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, data):
        self.sent.append(data)


@pytest.fixture()
def loop_in_thread():
    """A real, separately-running event loop — send_command_sync's whole
    point is bridging a synchronous caller (a FastAPI thread-pool thread)
    into async code running on *this* loop via
    asyncio.run_coroutine_threadsafe, so the test needs the real thing,
    not asyncio.run() on the calling thread."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_is_connected_reflects_register_and_unregister(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()

    assert registry.is_connected(1) is False
    registry.register(1, ws)
    assert registry.is_connected(1) is True
    registry.unregister(1)
    assert registry.is_connected(1) is False


def test_send_command_sync_raises_when_not_connected(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)

    with pytest.raises(AgentNotConnectedError):
        registry.send_command_sync(1, "list", timeout=1, path="")


def test_send_command_sync_round_trips_a_response(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)

    def responder():
        assert _wait_until(lambda: len(ws.sent) > 0), "command was never sent"
        sent = ws.sent[0]
        assert sent["type"] == "list"
        assert sent["path"] == "logs"
        loop_in_thread.call_soon_threadsafe(
            registry.resolve_response,
            1,
            {"type": "list_result", "id": sent["id"], "entries": [{"name": "a.log"}]},
        )

    thread = threading.Thread(target=responder)
    thread.start()
    try:
        result = registry.send_command_sync(1, "list", timeout=2, path="logs")
        assert result["entries"] == [{"name": "a.log"}]
    finally:
        thread.join(timeout=2)
        registry.unregister(1)


def test_send_command_sync_raises_agent_command_error_on_error_response(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)

    def responder():
        assert _wait_until(lambda: len(ws.sent) > 0)
        sent = ws.sent[0]
        loop_in_thread.call_soon_threadsafe(
            registry.resolve_response,
            1,
            {"type": "fetch_error", "id": sent["id"], "error": "permission denied"},
        )

    thread = threading.Thread(target=responder)
    thread.start()
    try:
        with pytest.raises(AgentCommandError, match="permission denied"):
            registry.send_command_sync(1, "fetch", timeout=2, path="secret.log")
    finally:
        thread.join(timeout=2)
        registry.unregister(1)


def test_send_command_sync_times_out_if_agent_never_responds(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)

    try:
        with pytest.raises(AgentTimeoutError):
            registry.send_command_sync(1, "list", timeout=0.2, path="")
    finally:
        registry.unregister(1)


def test_unregister_wakes_a_pending_call_with_not_connected_error(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)

    def disconnector():
        assert _wait_until(lambda: len(ws.sent) > 0)
        registry.unregister(1)

    thread = threading.Thread(target=disconnector)
    thread.start()
    try:
        with pytest.raises(AgentNotConnectedError):
            registry.send_command_sync(1, "list", timeout=5, path="")
    finally:
        thread.join(timeout=2)


def test_get_agent_registry_is_a_singleton():
    from app.agent_registry import get_agent_registry

    assert get_agent_registry() is get_agent_registry()
