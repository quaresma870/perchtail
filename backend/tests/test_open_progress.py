"""Progress and Cancel for a file opened whole: /open with a request_id can
be followed (GET /open-progress) and stopped (POST /open-cancel). A
cancelled open answers 409 read_cancelled, isn't audited as file.open,
leaves nothing in scratch and frees its connector slot; every copy loop
(copy_with_limit for SSH and SMB, WinRS, JEA, the push agent's
chunks, redaction) stops at its next block.

Every test that runs a request on a thread waits with a timeout and joins
the thread, and the slow fake source never waits more than a few seconds,
so nothing can hang."""

import asyncio
import io
import threading
import time

import app.collectors.ssh as ssh_module
import app.collectors.winrm as winrm_module
import pytest
from app import fetch_control, system_settings
from app.api.archive import _redact_file
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.api.errors import install as install_error_handlers
from app.auth.models import AuditLog, Role, User
from app.collectors import agent as agent_collector
from app.collectors import base as collector_base
from app.collectors import winrm_jea as jea
from app.collectors.base import FetchCancelledError
from app.db import get_session
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from app.redaction import DETECTOR_NAMES, compile_rules
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.test_agent_registry import FakeWebSocket, _chunk, loop_in_thread  # noqa: F401
from tests.test_collectors_ssh import FakeAttr, FakeSFTPClient, FakeSSHClient
from tests.test_collectors_winrm import (
    FakeResult,
    _read_output,
    _responses,
    fake_session,  # noqa: F401 - a fixture
)
from tests.test_collectors_winrm import _source as _winrm_source
from tests.test_collectors_winrm_jea import _source as _jea_source
from tests.test_collectors_winrm_jea import endpoint  # noqa: F401 - a fixture
from tests.test_redaction_endpoints import scratch_store  # noqa: F401 - a fixture

_BASE = "/var/log/appname"
CHUNK = 1024 * 1024
CONTENT = b"0123456789abcdef\n" * (3 * CHUNK // 17 + 1)
REQUEST = "0123456789abcdef0123456789abcdef"
WAIT = 5  # seconds: the longest anything here waits


class _Gate:
    """Holds the slow source's copy before each block but the first, until
    the test lets it go on (`go`) or lets every block through (`free`)."""

    def __init__(self):
        self.at_block = threading.Event()
        self.go = threading.Event()
        self.free = False
        self.closed = False


class _GatedRemoteFile(io.BytesIO):
    def __init__(self, content: bytes, gate: _Gate):
        super().__init__(content)
        self._gate = gate
        self._reads = 0

    def prefetch(self, file_size=None):
        pass

    def stat(self):
        return FakeAttr("", False, len(self.getvalue()))

    def read(self, size=-1):
        if self._reads > 0 and not self._gate.free:
            self._gate.at_block.set()
            assert self._gate.go.wait(WAIT), "the test never let the copy go on"
            self._gate.go.clear()
        self._reads += 1
        return super().read(size)

    def close(self):
        self._gate.closed = True
        super().close()


class _GatedSFTP(FakeSFTPClient):
    def __init__(self, gate: _Gate):
        super().__init__(files={f"{_BASE}/big.log": CONTENT}, dirs={_BASE})
        self._gate = gate

    def open(self, remote_path, mode="r"):
        assert mode == "rb"
        return _GatedRemoteFile(self._files[remote_path], self._gate)


@pytest.fixture()
def slow(
    session,
    scratch_store,  # noqa: F811
    monkeypatch,
):
    """(client, source, gate, users): an SSH source whose big.log (about
    3 MiB) copies one block at a time as the gate allows, opened by
    `users["admin"]` (switch `users["current"]` to act as another user),
    with one connector slot in all."""
    gate = _Gate()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(_GatedSFTP(gate)))
    fetch_control.clear()
    app = FastAPI()
    app.include_router(archive_router)
    install_error_handlers(app)
    app.dependency_overrides[get_session] = lambda: session
    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    admin = User(username="admin", role_id=role.id)
    other = User(username="other", role_id=role.id)
    session.add(admin)
    session.add(other)
    source = Source(
        name="web-01", protocol=Protocol.ssh, host="web-01", base_path=_BASE, credential_ref="x"
    )
    session.add(source)
    session.commit()
    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**/*",
            pattern_kind=PatternKind.glob,
        )
    )
    system_settings.set_int(session, "concurrency_global", 1)
    system_settings.set_int(session, "concurrency_per_source", 1)
    session.commit()
    users = {"admin": admin, "other": other, "current": admin}
    app.dependency_overrides[get_current_active_user] = lambda: users["current"]
    yield TestClient(app), source, gate, users
    gate.free = True
    gate.go.set()
    fetch_control.clear()


class _Background:
    """A request run on a thread; `result()` joins it with a timeout."""

    def __init__(self, call):
        self.response = None
        self.error: BaseException | None = None

        def run():
            try:
                self.response = call()
            except BaseException as exc:  # noqa: BLE001 - reported by result()
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        self.thread.join(timeout=WAIT * 2)
        assert not self.thread.is_alive(), "the request never ended"
        if self.error is not None:
            raise self.error
        return self.response


def _open(client, source, **params):
    return client.get(f"/sources/{source.id}/open", params={"path": "big.log", **params})


def _progress(client, source, request_id=REQUEST):
    return client.get(f"/sources/{source.id}/open-progress", params={"request_id": request_id})


def _cancel(client, source, request_id=REQUEST):
    return client.post(f"/sources/{source.id}/open-cancel", params={"request_id": request_id})


def _start_blocked_open(client, source, gate) -> _Background:
    opening = _Background(lambda: _open(client, source, request_id=REQUEST))
    assert gate.at_block.wait(WAIT), "the open never reached its second block"
    gate.at_block.clear()
    return opening


def _audits(session, action: str) -> list:
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


def test_progress_grows_as_the_copy_goes_on(slow, session):
    client, source, gate, _users = slow
    opening = _start_blocked_open(client, source, gate)
    try:
        first = _progress(client, source)
        assert first.status_code == 200
        assert first.json() == {"expected": len(CONTENT), "written": CHUNK, "phase": "copying"}
        assert first.headers["cache-control"] == "no-store"

        gate.go.set()
        assert gate.at_block.wait(WAIT)
        gate.at_block.clear()
        assert _progress(client, source).json()["written"] == 2 * CHUNK
    finally:
        gate.free = True
        gate.go.set()
    response = opening.result()
    assert response.status_code == 200
    assert response.content == CONTENT
    # Finished: the same 404 as an id that never was.
    assert _progress(client, source).status_code == 404
    # Reading the progress is never audited; the open is, once.
    assert len(_audits(session, "file.open")) == 1
    assert {row.action for row in session.exec(select(AuditLog)).all()} == {"file.open"}


def test_cancel_stops_the_copy_without_a_file_open_or_scratch_left(
    slow,
    session,
    scratch_store,  # noqa: F811
):
    client, source, gate, _users = slow
    opening = _start_blocked_open(client, source, gate)
    try:
        # The open holds the only connector slot meanwhile.
        busy = _open(client, source, request_id="f" * 32)
        assert busy.status_code == 503
        cancelled = _cancel(client, source)
        assert cancelled.status_code == 204
    finally:
        gate.go.set()
    response = opening.result()
    assert response.status_code == 409
    assert response.json() == {"detail": "The read was cancelled", "error_code": "read_cancelled"}
    assert _audits(session, "file.open") == []
    assert list(scratch_store.root.iterdir()) == []
    assert scratch_store._entries == {}
    # The source file was closed, and the slot is free again.
    assert gate.closed
    gate.free = True
    again = _open(client, source)
    assert again.status_code == 200
    assert again.content == CONTENT


@pytest.mark.parametrize("request_id", ["0" * 32, None])
def test_progress_and_cancel_answer_the_same_404_for_what_isnt_theirs(slow, request_id):
    """An id that isn't there, finished, someone else's or on another
    source: the same 404, and Cancel changes nothing."""
    client, source, gate, users = slow
    opening = _start_blocked_open(client, source, gate)
    try:
        expected = (404, {"detail": "Not found"})
        if request_id is None:
            # Another user asking for the admin's open.
            users["current"] = users["other"]
            request_id = REQUEST
        answers = [_progress(client, source, request_id), _cancel(client, source, request_id)]
        assert [(a.status_code, a.json()) for a in answers] == [expected, expected]
        users["current"] = users["admin"]
        assert _progress(client, source).status_code == 200
    finally:
        users["current"] = users["admin"]
        gate.free = True
        gate.go.set()
    assert opening.result().status_code == 200


def test_another_source_cant_see_the_open(slow, session):
    client, source, gate, _users = slow
    other = Source(name="db-01", protocol=Protocol.local, host="localhost", base_path="/tmp")
    session.add(other)
    session.commit()
    opening = _start_blocked_open(client, source, gate)
    try:
        assert _progress(client, other).status_code == 404
        assert _cancel(client, other).status_code == 404
    finally:
        gate.free = True
        gate.go.set()
    assert opening.result().status_code == 200


@pytest.mark.parametrize("request_id", ["short", "g" * 32, "0" * 31, "0" * 33, "ABCDEF" * 6])
def test_a_request_id_that_isnt_32_hex_characters_is_refused(slow, request_id):
    client, source, gate, _users = slow
    gate.free = True
    assert _open(client, source, request_id=request_id).status_code == 422
    assert _progress(client, source, request_id).status_code == 422
    assert _cancel(client, source, request_id).status_code == 422


def test_an_open_without_request_id_is_as_before(slow, session):
    client, source, gate, _users = slow
    gate.free = True
    response = _open(client, source)
    assert response.status_code == 200
    assert response.content == CONTENT
    assert "read_cancelled" not in response.text
    assert len(_audits(session, "file.open")) == 1


# -- fetch_control ---------------------------------------------------------------


def test_at_most_eight_controls_per_user_and_each_is_removed_when_its_open_ends():
    fetch_control.clear()
    try:
        ids = [f"{n:032x}" for n in range(9)]
        with fetch_control.controlling(1, 7, ids[0]):
            assert fetch_control.current.get() is not None
        assert fetch_control.find(1, 7, ids[0]) is None
        assert fetch_control.current.get() is None

        held = [fetch_control._register(1, 7, request_id) for request_id in ids[:8]]
        assert all(held)
        # A ninth open runs as before, without progress.
        with fetch_control.controlling(1, 7, ids[8]):
            assert fetch_control.current.get() is None
        # Another user still gets one.
        with fetch_control.controlling(2, 7, ids[8]):
            assert fetch_control.current.get() is not None
        with pytest.raises(RuntimeError), fetch_control.controlling(2, 7, ids[8]):
            raise RuntimeError("the open failed")
        assert fetch_control.find(2, 7, ids[8]) is None
    finally:
        fetch_control.clear()


def test_without_a_control_the_copy_is_unchanged(tmp_path):
    destination = tmp_path / "out"
    assert collector_base.copy_with_limit(io.BytesIO(CONTENT), destination, len(CONTENT)) == len(
        CONTENT
    )
    assert destination.read_bytes() == CONTENT


class _CancelAfterFirstBlock(fetch_control.FetchControl):
    """Cancel pressed as soon as the first block is written."""

    def check(self) -> None:
        if self.written > 0:
            self.cancel.set()
        super().check()


def _with_control(control):
    token = fetch_control.current.set(control)
    return lambda: fetch_control.current.reset(token)


def test_copy_with_limit_stops_after_a_cancel_and_deletes_the_partial_file(tmp_path):
    """SSH and SMB copy through copy_with_limit."""
    control = _CancelAfterFirstBlock(user_id=1, source_id=1)
    reset = _with_control(control)
    destination = tmp_path / "out"
    try:
        with pytest.raises(FetchCancelledError):
            collector_base.copy_with_limit(io.BytesIO(CONTENT), destination, len(CONTENT))
    finally:
        reset()
    assert control.written == CHUNK
    assert not destination.exists()


def test_redaction_says_so_and_stops_after_a_cancel(tmp_path):
    original = tmp_path / "plain.log"
    original.write_bytes(b"user=alice@example.com\n" * 1000)
    control = fetch_control.FetchControl(user_id=1, source_id=1)
    control.cancel.set()
    reset = _with_control(control)
    try:
        with pytest.raises(FetchCancelledError):
            _redact_file(original, tmp_path / "out", _redaction(), final=True)
    finally:
        reset()
    assert control.phase == "redacting"


def _redaction():
    return compile_rules([{"name": name, "detector": name} for name in DETECTOR_NAMES])


# -- WinRS, JEA and the push agent -----------------------------------------------


def test_winrs_stops_between_receives_and_ends_the_command(
    fake_session,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(winrm_module, "_SUB_BLOCK_BYTES", 4)
    content = b"0123456789abcdef"

    def handler(script):
        return FakeResult(std_out=str(len(content)).encode())

    fake_session.handler = handler
    fake_session.stream_handler = lambda script: _responses(_read_output(content, script), cut=8)
    control = _CancelAfterFirstBlock(user_id=1, source_id=1)
    reset = _with_control(control)
    destination = tmp_path / "out"
    try:
        with pytest.raises(FetchCancelledError):
            winrm_module.fetch_file(_winrm_source(), "app.log", destination)
    finally:
        reset()
    assert control.expected == len(content)
    assert 0 < control.written < len(content)
    events = fake_session.protocol.events
    # Stopped part way, and the command, shell and session were ended as
    # after an error.
    (script,) = fake_session.streamed
    assert events.count("receive") < len(_responses(_read_output(content, script), cut=8))
    assert events[-3:] == ["cleanup_command", "close_shell", "close_session"]
    assert not destination.exists()


def test_jea_stops_between_blocks(
    endpoint,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(jea, "_CHUNK_BYTES", 5)
    control = _CancelAfterFirstBlock(user_id=1, source_id=1)
    reset = _with_control(control)
    destination = tmp_path / "out"
    try:
        with pytest.raises(FetchCancelledError):
            winrm_module.fetch_file(_jea_source(), "app.log", destination)
    finally:
        reset()
    blocks = [p["Offset"] for c, p in endpoint.calls if c == "Read-PerchTailBlock"]
    assert blocks == [0]
    assert control.written == 5
    assert not destination.exists()


def test_the_agent_is_sent_cancel_and_the_open_stops(
    loop_in_thread,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    from app.agent_registry import FEATURE_LINKS, AgentRegistry

    registry = AgentRegistry()
    registry.bind_loop(loop_in_thread)
    ws = FakeWebSocket()
    registry.register(1, ws, frozenset({FEATURE_LINKS}))
    monkeypatch.setattr(agent_collector, "get_agent_registry", lambda: registry)
    monkeypatch.setattr(agent_collector, "require_links", lambda source: None)
    source = Source(id=1, name="files-01", protocol=Protocol.agent, host="files-01", base_path="/")
    control = fetch_control.FetchControl(user_id=1, source_id=1)
    first_chunk_written = threading.Event()

    def agent_side():
        deadline = time.monotonic() + WAIT
        while not ws.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        request_id = ws.sent[0]["id"]
        asyncio.run_coroutine_threadsafe(
            registry.receive_chunk(1, _chunk(0, b"abcd")(request_id)), loop_in_thread
        ).result(timeout=WAIT)
        first_chunk_written.set()
        control.cancel.set()
        asyncio.run_coroutine_threadsafe(
            registry.receive_chunk(1, _chunk(1, b"efgh")(request_id)), loop_in_thread
        ).result(timeout=WAIT)

    thread = threading.Thread(target=agent_side, daemon=True)
    thread.start()
    reset = _with_control(control)
    destination = tmp_path / "out"
    try:
        with pytest.raises(FetchCancelledError):
            agent_collector.fetch_file(source, "big.log", destination)
    finally:
        reset()
        thread.join(timeout=WAIT * 2)
        registry.unregister(1)
    assert not thread.is_alive()
    assert first_chunk_written.is_set()
    assert control.written == 4
    assert {"type": "cancel", "id": ws.sent[0]["id"]} in ws.sent
    assert not destination.exists()
