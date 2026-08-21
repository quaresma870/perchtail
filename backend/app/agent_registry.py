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
import threading
import uuid
from dataclasses import dataclass, field
from functools import lru_cache

from fastapi import WebSocket

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
    `*_error` message."""


@dataclass
class _AgentConnection:
    websocket: WebSocket
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


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

    def register(self, source_id: int, websocket: WebSocket) -> None:
        with self._lock:
            self._connections[source_id] = _AgentConnection(websocket=websocket)
        logger.info("agent.connected", source_id=source_id)

    def unregister(self, source_id: int) -> None:
        with self._lock:
            connection = self._connections.pop(source_id, None)
        if connection is None:
            return
        # Anything still awaiting a response when the agent drops must be
        # woken up with an error rather than hanging until its own timeout.
        for future in connection.pending.values():
            if not future.done():
                future.set_exception(AgentNotConnectedError("agent disconnected"))
        logger.info("agent.disconnected", source_id=source_id)

    def resolve_response(self, source_id: int, message: dict) -> None:
        """Called from the WS endpoint's receive loop (already on the main
        event loop) when the agent sends a `*_result`/`*_error` message."""
        with self._lock:
            connection = self._connections.get(source_id)
        if connection is None:
            return
        future = connection.pending.get(message.get("id", ""))
        if future is None or future.done():
            return
        if message.get("type", "").endswith("_error"):
            future.set_exception(AgentCommandError(message.get("error", "agent command failed")))
        else:
            future.set_result(message)

    async def _send_command(self, source_id: int, command: dict, timeout: float) -> dict:
        with self._lock:
            connection = self._connections.get(source_id)
        if connection is None:
            raise AgentNotConnectedError(f"no agent connected for source {source_id}")

        request_id = command["id"]
        future: asyncio.Future = self._loop.create_future()
        connection.pending[request_id] = future
        try:
            await connection.websocket.send_json(command)
            try:
                return await asyncio.wait_for(future, timeout)
            except TimeoutError as exc:
                raise AgentTimeoutError(
                    f"agent for source {source_id} did not respond in time"
                ) from exc
        finally:
            connection.pending.pop(request_id, None)

    def send_command_sync(
        self, source_id: int, command_type: str, timeout: float = DEFAULT_TIMEOUT_SECONDS, **fields
    ) -> dict:
        """The entry point collectors/agent.py actually calls — runs on
        whatever thread FastAPI's thread pool put the sync path operation
        on, schedules the real work onto the main event loop, and blocks
        this thread until it completes."""
        if self._loop is None:
            raise AgentNotConnectedError("agent registry has no event loop bound yet")

        command = {"type": command_type, "id": str(uuid.uuid4()), **fields}
        future = asyncio.run_coroutine_threadsafe(
            self._send_command(source_id, command, timeout), self._loop
        )
        # A few seconds of slack beyond the in-loop timeout so a slow
        # scheduler doesn't race a TimeoutError against this call's own —
        # the inner asyncio.wait_for is the real deadline.
        return future.result(timeout=timeout + 5)


@lru_cache
def get_agent_registry() -> AgentRegistry:
    return AgentRegistry()
