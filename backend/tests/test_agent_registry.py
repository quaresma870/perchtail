import asyncio
import threading
import time

import pytest
from app.agent_registry import (
    FEATURE_REDACTION,
    AgentCommandError,
    AgentError,
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


def test_a_replaced_connection_closing_does_not_unregister_its_successor(loop_in_thread):
    """The agent reconnects before the server notices the old socket is
    dead; when the old socket's receive loop finally ends, it must not take
    the new connection down with it."""
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    old, new = FakeWebSocket(), FakeWebSocket()
    registry.register(1, old)
    assert registry.register(1, new).websocket is old

    registry.unregister(1, old)

    assert registry.is_connected(1)

    def responder():
        assert _wait_until(lambda: len(new.sent) > 0)
        command = new.sent[0]
        reply = {"type": "list_result", "id": command["id"], "entries": []}
        # The stale socket answering the same id must be ignored.
        registry.resolve_response(1, {**reply, "entries": ["stale"]}, old)
        registry.resolve_response(1, reply, new)

    thread = threading.Thread(target=responder)
    thread.start()
    try:
        result = registry.send_command_sync(1, "list", timeout=5, path="")
    finally:
        thread.join(timeout=2)
    assert result["entries"] == []
    assert old.sent == []

    registry.unregister(1, new)
    assert not registry.is_connected(1)


def test_a_replacement_from_another_address_is_kept_as_a_warning(loop_in_thread):
    """Two agents may be using one token: a connection replaced by one from
    another address leaves a warning, kept until a connection that replaces
    none."""
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    registry.register(1, FakeWebSocket(), address="198.51.100.1")
    assert registry.connection_replaced(1) is None

    # The same agent reconnecting from where it was: no warning.
    registry.register(1, FakeWebSocket(), address="198.51.100.1")
    assert registry.connection_replaced(1) is None

    registry.register(1, FakeWebSocket(), address="203.0.113.9")
    warning = registry.connection_replaced(1)
    assert (warning.previous_address, warning.address) == ("198.51.100.1", "203.0.113.9")
    assert registry.connection_replaced(2) is None

    # A replacement from the same address doesn't clear it...
    registry.register(1, FakeWebSocket(), address="203.0.113.9")
    assert registry.connection_replaced(1) == warning
    # ...a connection that replaces none does.
    registry.unregister(1)
    registry.register(1, FakeWebSocket(), address="203.0.113.9")
    assert registry.connection_replaced(1) is None
    registry.unregister(1)


def test_replacing_a_connection_fails_its_pending_commands_immediately(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    old = FakeWebSocket()
    registry.register(1, old)

    def reconnector():
        assert _wait_until(lambda: len(old.sent) > 0)
        loop_in_thread.call_soon_threadsafe(registry.register, 1, FakeWebSocket())

    thread = threading.Thread(target=reconnector)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(AgentNotConnectedError, match="replaced"):
            registry.send_command_sync(1, "list", timeout=10, path="")
    finally:
        thread.join(timeout=2)
    assert time.monotonic() - started < 5


# --- #205: streamed fetches -------------------------------------------------


def _streamed_fetch(loop, registry, ws, messages, *, timeout=2, limit=None, delay=0.0):
    """Runs a streamed fetch against `registry`, with the agent's side
    sending `messages` (built from the command id) as they'd arrive."""
    import io

    sink = io.BytesIO()

    def agent():
        assert _wait_until(lambda: len(ws.sent) > 0)
        request_id = ws.sent[0]["id"]
        for build in messages:
            message = build(request_id)
            if message.get("type") == "fetch_chunk":
                asyncio.run_coroutine_threadsafe(registry.receive_chunk(1, message), loop).result(
                    timeout=2
                )
            else:
                loop.call_soon_threadsafe(registry.resolve_response, 1, message)
            time.sleep(delay)

    thread = threading.Thread(target=agent)
    thread.start()
    try:
        result = registry.send_command_sync(
            1, "fetch", timeout=timeout, sink=sink, limit=limit, path="big.log", chunk_size=4
        )
        return result, sink.getvalue(), ws.sent[0]
    finally:
        thread.join(timeout=5)


def _chunk(seq, data: bytes):
    import base64

    return lambda request_id: {
        "type": "fetch_chunk",
        "id": request_id,
        "seq": seq,
        "data_b64": base64.b64encode(data).decode(),
    }


def _end(chunks, size):
    return lambda request_id: {
        "type": "fetch_result",
        "id": request_id,
        "streamed": True,
        "chunks": chunks,
        "size": size,
    }


@pytest.fixture()
def streaming_registry(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)
    yield registry, ws
    registry.unregister(1)


def test_a_streamed_fetch_writes_each_chunk_to_the_sink(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    result, written, command = _streamed_fetch(
        loop_in_thread,
        registry,
        ws,
        [_chunk(0, b"abcd"), _chunk(1, b"efgh"), _chunk(2, b"ij"), _end(3, 10)],
    )
    assert written == b"abcdefghij"
    assert result["streamed"] is True
    assert command["chunk_size"] == 4 and "sink" not in command


def test_a_streamed_fetch_over_the_limit_is_too_large(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    with pytest.raises(AgentCommandError) as caught:
        _streamed_fetch(
            loop_in_thread,
            registry,
            ws,
            [_chunk(0, b"abcd"), _chunk(1, b"efgh"), _end(2, 8)],
            limit=6,
        )
    assert (caught.value.code, caught.value.size) == ("too_large", 8)


def test_the_agents_too_large_error_keeps_its_code_and_size(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    refusal = lambda request_id: {  # noqa: E731
        "type": "fetch_error",
        "id": request_id,
        "error": "file is 5000 bytes, over the 4000-byte limit",
        "code": "too_large",
        "size": 5000,
    }
    with pytest.raises(AgentCommandError) as caught:
        _streamed_fetch(loop_in_thread, registry, ws, [refusal], limit=4000)
    assert (caught.value.code, caught.value.size) == ("too_large", 5000)


def test_chunks_out_of_order_fail_the_fetch(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    with pytest.raises(AgentError, match="out of order"):
        _streamed_fetch(
            loop_in_thread, registry, ws, [_chunk(0, b"abcd"), _chunk(2, b"ij"), _end(2, 6)]
        )


def test_a_stream_that_ends_short_fails_the_fetch(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    with pytest.raises(AgentError, match="incomplete"):
        _streamed_fetch(loop_in_thread, registry, ws, [_chunk(0, b"abcd"), _end(2, 8)])


def test_a_stream_times_out_only_when_chunks_stop(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    # Five chunks 0.3 s apart take longer than the 1 s timeout in all,
    # but no single wait does.
    chunks = [_chunk(seq, b"abcd") for seq in range(5)]
    result, written, _ = _streamed_fetch(
        loop_in_thread, registry, ws, [*chunks, _end(5, 20)], timeout=1.0, delay=0.3
    )
    assert len(written) == 20 and result["streamed"] is True


def test_a_stalled_stream_times_out(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    with pytest.raises(AgentTimeoutError):
        _streamed_fetch(loop_in_thread, registry, ws, [_chunk(0, b"abcd")], timeout=0.3)


def test_giving_up_on_a_stream_tells_the_agent_to_cancel(loop_in_thread, streaming_registry):
    """Review of #205: without it the agent read the file to the end."""
    registry, ws = streaming_registry
    with pytest.raises(AgentCommandError):
        _streamed_fetch(
            loop_in_thread,
            registry,
            ws,
            [_chunk(0, b"abcd"), _chunk(1, b"efgh"), _end(2, 8)],
            limit=6,
        )
    assert {"type": "cancel", "id": ws.sent[0]["id"]} in ws.sent


def test_a_completed_stream_sends_no_cancel(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    _streamed_fetch(loop_in_thread, registry, ws, [_chunk(0, b"abcd"), _end(1, 4)])
    assert [message["type"] for message in ws.sent] == ["fetch"]


def test_an_unknown_command_error_keeps_its_type(loop_in_thread):
    # #319: an agent older than 0.7 answers "upgrade" with unknown_error,
    # which the server reports as "too old to upgrade remotely".
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)

    def responder():
        assert _wait_until(lambda: len(ws.sent) > 0)
        loop_in_thread.call_soon_threadsafe(
            registry.resolve_response,
            1,
            {"type": "unknown_error", "id": ws.sent[0]["id"], "error": "unknown command"},
        )

    thread = threading.Thread(target=responder)
    thread.start()
    try:
        with pytest.raises(AgentCommandError) as caught:
            registry.send_command_sync(
                1, "upgrade", timeout=2, command_id="upgrade-1", version="0.7.0"
            )
        assert caught.value.message_type == "unknown_error"
        assert ws.sent[0] == {"type": "upgrade", "id": "upgrade-1", "version": "0.7.0"}
    finally:
        thread.join(timeout=2)
        registry.unregister(1)


# --- keepalive (#333) -------------------------------------------------------


def test_the_keepalive_drops_a_silent_agent(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)
    started = time.monotonic()
    watch = asyncio.run_coroutine_threadsafe(
        registry.watch(1, ws, interval=0.05, dead_after=0.3), loop_in_thread
    )
    assert watch.result(timeout=3) is True
    assert time.monotonic() - started >= 0.3
    assert not registry.is_connected(1)
    # It pinged before giving up.
    assert any(message["type"] == "ping" for message in ws.sent)


def test_the_keepalive_keeps_an_agent_that_answers(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)
    watch = asyncio.run_coroutine_threadsafe(
        registry.watch(1, ws, interval=0.05, dead_after=0.3), loop_in_thread
    )
    deadline = time.monotonic() + 0.9
    while time.monotonic() < deadline:
        registry.touch(1, ws)  # the agent's pongs and messages
        time.sleep(0.05)
    assert not watch.done()
    assert registry.is_connected(1)
    registry.unregister(1)
    assert watch.result(timeout=3) is False


def test_the_keepalive_of_a_replaced_connection_just_ends(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    old, new = FakeWebSocket(), FakeWebSocket()
    registry.register(1, old)
    watch = asyncio.run_coroutine_threadsafe(
        registry.watch(1, old, interval=0.05, dead_after=10), loop_in_thread
    )
    registry.register(1, new)
    assert watch.result(timeout=3) is False
    assert registry.is_connected(1)
    registry.unregister(1)


def test_a_failed_ping_drops_the_connection(loop_in_thread):
    class BrokenWebSocket(FakeWebSocket):
        async def send_json(self, data):
            raise RuntimeError("connection reset")

    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = BrokenWebSocket()
    registry.register(1, ws)
    watch = asyncio.run_coroutine_threadsafe(
        registry.watch(1, ws, interval=0.05, dead_after=10), loop_in_thread
    )
    assert watch.result(timeout=3) is True
    assert not registry.is_connected(1)


# --- #382, #249: what an agent can do, and fetch_archive ---------------------


def test_features_are_those_of_the_live_connection(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    assert registry.features(1) == frozenset()
    assert not registry.redacts_on_host(1)

    registry.register(1, FakeWebSocket(), frozenset({FEATURE_REDACTION}))
    assert registry.redacts_on_host(1)

    # An older agent replacing it brings no feature with it.
    registry.register(1, FakeWebSocket())
    assert not registry.redacts_on_host(1)
    registry.unregister(1)
    assert registry.features(1) == frozenset()


def test_the_fallback_to_the_server_is_reported_once_per_connection(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    assert registry.first_redaction_fallback(1) is False  # nothing connected
    registry.register(1, FakeWebSocket())
    assert registry.first_redaction_fallback(1) is True
    assert registry.first_redaction_fallback(1) is False
    registry.register(1, FakeWebSocket())
    assert registry.first_redaction_fallback(1) is True
    registry.unregister(1)


def test_a_command_that_needs_a_feature_is_never_sent_without_it(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws)
    with pytest.raises(AgentCommandError) as caught:
        registry.send_command_sync(
            1, "fetch", timeout=1, path="app.log", redact={}, require_feature=FEATURE_REDACTION
        )
    assert caught.value.code == "feature_missing"
    assert ws.sent == []
    registry.unregister(1)


def test_a_redaction_error_keeps_its_code_and_rule(loop_in_thread):
    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws, frozenset({FEATURE_REDACTION}))

    def agent():
        assert _wait_until(lambda: len(ws.sent) > 0)
        message = {
            "type": "redaction_error",
            "id": ws.sent[0]["id"],
            "error": "the rule can't run",
            "code": "rule_unsupported",
            "rule": 31,
        }
        loop_in_thread.call_soon_threadsafe(registry.resolve_response, 1, message)

    thread = threading.Thread(target=agent)
    thread.start()
    try:
        with pytest.raises(AgentCommandError) as caught:
            registry.send_command_sync(1, "fetch", timeout=2, path="app.log")
    finally:
        thread.join(timeout=5)
    assert caught.value.message_type == "redaction_error"
    assert caught.value.code == "rule_unsupported"
    assert caught.value.rule == 31
    registry.unregister(1)


class _Members:
    """An ArchiveSink that keeps each member's content in memory."""

    def __init__(self):
        self.members: list[tuple[dict, bytearray]] = []

    def start_member(self, message):
        if message.get("name") == "refused":
            raise ValueError("not asked for")
        self.members.append((message, bytearray()))

    def write(self, data):
        if not self.members:
            raise ValueError("no member")
        self.members[-1][1].extend(data)
        return len(data)


def _member(seq, name, **extra):
    return lambda request_id: {
        "type": "archive_member",
        "id": request_id,
        "seq": seq,
        "name": name,
        **extra,
    }


def _fetch_archive(loop, registry, ws, messages, sink, archive=True):
    def agent():
        assert _wait_until(lambda: len(ws.sent) > 0)
        request_id = ws.sent[0]["id"]
        for build in messages:
            message = build(request_id)
            kind = message.get("type")
            if kind == "fetch_chunk":
                coroutine = registry.receive_chunk(1, message)
            elif kind == "archive_member":
                coroutine = registry.receive_archive_member(1, message)
            else:
                loop.call_soon_threadsafe(registry.resolve_response, 1, message)
                continue
            asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=2)

    thread = threading.Thread(target=agent)
    thread.start()
    try:
        return registry.send_command_sync(
            1, "fetch_archive", timeout=2, sink=sink, archive=archive, path="logs.zip"
        )
    finally:
        thread.join(timeout=5)


def test_a_fetch_archive_hands_each_member_to_the_sink(loop_in_thread, streaming_registry):
    registry, ws = streaming_registry
    sink = _Members()
    result = _fetch_archive(
        loop_in_thread,
        registry,
        ws,
        [
            _member(0, "a.log"),
            _chunk(1, b"abcd"),
            _chunk(2, b"ef"),
            _member(3, "gone.log", skipped="not_found"),
            _member(4, "b.log"),
            _chunk(5, b"gh"),
            # chunks counts the fetch_chunk messages only.
            _end(3, 8),
        ],
        sink,
    )
    assert result["streamed"] is True
    assert [(m["name"], bytes(data)) for m, data in sink.members] == [
        ("a.log", b"abcdef"),
        ("gone.log", b""),
        ("b.log", b"gh"),
    ]


@pytest.mark.parametrize(
    "messages",
    [
        # Content before any member.
        [_chunk(0, b"abcd"), _end(1, 4)],
        # A member out of order.
        [_member(1, "a.log"), _end(0, 0)],
        # A member the sink refuses.
        [_member(0, "refused"), _end(0, 0)],
        # Chunks counted wrong at the end.
        [_member(0, "a.log"), _chunk(1, b"ab"), _end(2, 2)],
    ],
)
def test_a_fetch_archive_out_of_line_fails(loop_in_thread, streaming_registry, messages):
    registry, ws = streaming_registry
    with pytest.raises(AgentError):
        _fetch_archive(loop_in_thread, registry, ws, messages, _Members())


def test_an_archive_member_for_a_plain_fetch_fails_it(loop_in_thread, streaming_registry):
    import io

    registry, ws = streaming_registry
    with pytest.raises(AgentError):
        _fetch_archive(
            loop_in_thread,
            registry,
            ws,
            [_member(0, "a.log"), _chunk(1, b"ab"), _end(1, 2)],
            io.BytesIO(),
            archive=False,
        )
