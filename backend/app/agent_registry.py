"""Tracks live push-agent WebSocket connections (Phase 2 — see ROADMAP.md
and Protocol.agent's docstring in app/models.py) and lets synchronous
connector code (collectors/agent.py, called from FastAPI's thread pool like
every other connector) send a command to a connected agent and block for its
response, even though the WebSocket itself is only ever touched from the
main asyncio event loop.

Bookkeeping is in-memory only, not in SQLite — exactly like scratch.py's
ScratchStore, and for the same reason: a connection is inherently
per-process and shouldn't (can't) survive a restart."""

import asyncio
import base64
import binascii
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import BinaryIO

from fastapi import WebSocket

from app.json_values import as_int
from app.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0


class AgentError(Exception):
    """Base for every agent-connector failure — collectors/agent.py lets
    these propagate uncaught, same as the other connectors let their own
    protocol-client exceptions propagate (see api/archive.py)."""


class AgentNotConnectedError(AgentError):
    pass


class AgentTimeoutError(AgentError):
    pass


class AgentCommandError(AgentError):
    """The agent itself reported a failure (e.g. path not found) via a
    `*_error` message. `code` is "too_large" (with `size`) when a streamed
    fetch is over the limit (#205); None otherwise, and from older agents."""

    def __init__(self, message: str, *, code: str | None = None, size: int | None = None):
        super().__init__(message)
        self.code = code
        self.size = size


@dataclass
class _Stream:
    """A fetch the agent sends in `fetch_chunk` messages (#205), written to
    `sink` as each chunk arrives so the file is never held in memory."""

    sink: BinaryIO
    limit: int | None
    next_seq: int = 0
    written: int = 0
    last_activity: float = field(default_factory=time.monotonic)


@dataclass
class _AgentConnection:
    websocket: WebSocket
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    streams: dict[str, _Stream] = field(default_factory=dict)


def _fail_pending(connection: _AgentConnection, reason: str) -> None:
    for future in connection.pending.values():
        if not future.done():
            future.set_exception(AgentNotConnectedError(reason))


class AgentRegistry:
    def __init__(self):
        self._connections: dict[int, _AgentConnection] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once at startup (app.main's lifespan, which runs on the
        main event loop) so send_command_sync knows where to schedule the
        coroutine from an arbitrary worker thread."""
        self._loop = loop

    def is_connected(self, source_id: int) -> bool:
        with self._lock:
            return source_id in self._connections

    def connected_count(self) -> int:
        """Exposed for the detailed health endpoint (app/api/monitoring.py)
        to report connected-vs-configured agent sources."""
        with self._lock:
            return len(self._connections)

    def register(self, source_id: int, websocket: WebSocket) -> "_AgentConnection | None":
        """Makes `websocket` the source's connection. If one was already
        registered (the agent reconnected before the old socket was noticed
        as dead, or a second agent is using the same token), it's replaced:
        its pending commands fail now instead of waiting out their timeout,
        and it is returned so the caller can close it."""
        with self._lock:
            replaced = self._connections.get(source_id)
            self._connections[source_id] = _AgentConnection(websocket=websocket)
        if replaced is not None:
            _fail_pending(replaced, "agent connection replaced by a newer one")
            logger.warning("agent.connection_replaced", source_id=source_id)
        logger.info("agent.connected", source_id=source_id)
        return replaced

    def unregister(self, source_id: int, websocket: WebSocket | None = None) -> None:
        """Removes the source's connection -- but, when `websocket` is given,
        only if it is still the registered one. A replaced connection's
        receive loop ends after its successor registered, and must not take
        the successor down with it (the source would show as disconnected
        while its agent is connected)."""
        with self._lock:
            connection = self._connections.get(source_id)
            if connection is None:
                return
            if websocket is not None and connection.websocket is not websocket:
                return
            del self._connections[source_id]
        # Anything still awaiting a response when the agent drops must be
        # woken up with an error rather than hanging until its own timeout.
        _fail_pending(connection, "agent disconnected")
        logger.info("agent.disconnected", source_id=source_id)

    def resolve_response(
        self, source_id: int, message: dict, websocket: WebSocket | None = None
    ) -> None:
        """Called from the WS endpoint's receive loop (already on the main
        event loop) when the agent sends a `*_result`/`*_error` message.
        With `websocket`, a message from a connection that has since been
        replaced is ignored: only the current connection answers commands."""
        with self._lock:
            connection = self._connections.get(source_id)
        if connection is None:
            return
        if websocket is not None and connection.websocket is not websocket:
            return
        request_id = message.get("id", "")
        future = connection.pending.get(request_id)
        if future is None or future.done():
            return
        if message.get("type", "").endswith("_error"):
            code = message.get("code")
            future.set_exception(
                AgentCommandError(
                    str(message.get("error", "agent command failed")),
                    code=code if isinstance(code, str) else None,
                    size=as_int(message.get("size")),
                )
            )
            return
        stream = connection.streams.get(request_id)
        if stream is not None and message.get("streamed"):
            complete = as_int(message.get("chunks")) == stream.next_seq and (
                as_int(message.get("size")) == stream.written
            )
            if not complete:
                future.set_exception(AgentError("agent's streamed fetch ended incomplete"))
                return
        future.set_result(message)

    async def receive_chunk(
        self, source_id: int, message: dict, websocket: WebSocket | None = None
    ) -> None:
        """Called from the WS endpoint's receive loop for each `fetch_chunk`
        of a streamed fetch (#205): writes it to the command's sink. The
        write is awaited before the next message is read, so a slow disk
        slows the agent down (TCP backpressure) instead of piling chunks up
        in memory. Any problem fails the command, never the connection."""
        with self._lock:
            connection = self._connections.get(source_id)
        if connection is None:
            return
        if websocket is not None and connection.websocket is not websocket:
            return
        request_id = message.get("id", "")
        future = connection.pending.get(request_id)
        stream = connection.streams.get(request_id)
        if future is None or future.done() or stream is None:
            return

        if as_int(message.get("seq")) != stream.next_seq:
            future.set_exception(AgentError("agent sent fetch chunks out of order"))
            return
        try:
            data = base64.b64decode(str(message.get("data_b64", "")), validate=True)
        except (binascii.Error, ValueError):
            future.set_exception(AgentError("agent sent an invalid fetch chunk"))
            return
        if stream.limit is not None and stream.written + len(data) > stream.limit:
            future.set_exception(
                AgentCommandError(
                    "file is over the fetch limit",
                    code="too_large",
                    size=stream.written + len(data),
                )
            )
            return
        try:
            await asyncio.to_thread(stream.sink.write, data)
        except (OSError, ValueError) as exc:  # disk full; sink closed after a timeout
            if not future.done():
                future.set_exception(AgentError(f"could not write the fetched file: {exc}"))
            return
        stream.written += len(data)
        stream.next_seq += 1
        stream.last_activity = time.monotonic()

    async def _send_command(
        self,
        source_id: int,
        command: dict,
        timeout: float,
        sink: BinaryIO | None = None,
        limit: int | None = None,
    ) -> dict:
        with self._lock:
            connection = self._connections.get(source_id)
        if connection is None:
            raise AgentNotConnectedError(f"no agent connected for source {source_id}")

        request_id = command["id"]
        future: asyncio.Future = self._loop.create_future()
        connection.pending[request_id] = future
        stream = None
        completed = False
        if sink is not None:
            stream = _Stream(sink=sink, limit=limit)
            connection.streams[request_id] = stream
        try:
            await connection.websocket.send_json(command)
            if stream is None:
                try:
                    return await asyncio.wait_for(future, timeout)
                except TimeoutError as exc:
                    raise AgentTimeoutError(
                        f"agent for source {source_id} did not respond in time"
                    ) from exc
            # A streamed fetch of a large file can take much longer than
            # `timeout` in all; it only times out when no chunk has arrived
            # for that long.
            while True:
                remaining = stream.last_activity + timeout - time.monotonic()
                if remaining <= 0:
                    raise AgentTimeoutError(
                        f"agent for source {source_id} stopped sending the file"
                    )
                done, _ = await asyncio.wait({future}, timeout=remaining)
                if done:
                    result = future.result()
                    completed = True
                    return result
        finally:
            connection.pending.pop(request_id, None)
            connection.streams.pop(request_id, None)
            if stream is not None and not completed:
                # Gave up (timeout, size limit, bad chunk): tell the agent
                # to stop reading the file. Best effort; an agent older
                # than 0.5.3 answers with an unknown_error nobody awaits.
                try:
                    await connection.websocket.send_json({"type": "cancel", "id": request_id})
                except Exception:  # noqa: BLE001 - the connection may be gone
                    pass

    def send_command_sync(
        self,
        source_id: int,
        command_type: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        sink: BinaryIO | None = None,
        limit: int | None = None,
        **fields,
    ) -> dict:
        """The entry point collectors/agent.py actually calls — runs on
        whatever thread FastAPI's thread pool put the sync path operation
        on, schedules the real work onto the main event loop, and blocks
        this thread until it completes.

        With `sink`, `fetch_chunk` messages for this command are written to
        it as they arrive, refusing more than `limit` bytes (#205); the
        timeout then applies to the wait for each chunk, not to the whole
        transfer."""
        if self._loop is None:
            raise AgentNotConnectedError("agent registry has no event loop bound yet")

        command = {"type": command_type, "id": str(uuid.uuid4()), **fields}
        future = asyncio.run_coroutine_threadsafe(
            self._send_command(source_id, command, timeout, sink=sink, limit=limit), self._loop
        )
        if sink is not None:
            # No overall deadline: the in-loop wait times out when chunks stop.
            return future.result()
        # A few seconds of slack beyond the in-loop timeout so a slow
        # scheduler doesn't race a TimeoutError against this call's own —
        # the inner asyncio.wait_for is the real deadline.
        return future.result(timeout=timeout + 5)


@lru_cache
def get_agent_registry() -> AgentRegistry:
    return AgentRegistry()
