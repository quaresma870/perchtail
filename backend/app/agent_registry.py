"""Tracks live push-agent WebSocket connections (see docs/design-decisions.md,
"Push agent", and Protocol.agent's docstring in app/models.py) and lets synchronous
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
from datetime import datetime
from functools import lru_cache
from typing import BinaryIO, Protocol

from fastapi import WebSocket

from app.json_values import as_int
from app.logging_config import get_logger
from app.timeutils import utcnow

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0

# Keepalive (#333): the server pings each agent this often, and drops a
# connection it has heard nothing from (no answer, no message) for
# AGENT_DEAD_AFTER_SECONDS -- a half-open connection a NAT or firewall
# dropped silently would otherwise keep showing the agent as connected.
# Any message counts, so agents older than 0.7, which answer "ping" with an
# unknown_error, are kept as well.
AGENT_PING_INTERVAL_SECONDS = 30.0
AGENT_DEAD_AFTER_SECONDS = 90.0


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
    fetch is over the limit (#205); None otherwise, and from older agents.
    With "too_large", `limit` is which limit the agent says was passed:
    "fetch" (max_bytes) or "decompressed" (max_decompressed); None from an
    agent that doesn't say.
    `message_type` is the message's own type: "unknown_error" means the
    agent doesn't know the command (#319: an agent older than 0.7 asked to
    upgrade); "redaction_error" that it refused to redact (#382, #249), with
    `rule` the id of the rule it can't run, when that's why. Code
    "link_target" (FEATURE_LINKS) refuses a path that isn't where the
    command expected: `real` is where it is, relative to the base path, or
    `outside` says it leads out of the base path."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        size: int | None = None,
        message_type: str | None = None,
        rule: int | None = None,
        real: str | None = None,
        outside: bool = False,
        limit: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.size = size
        self.limit = limit
        self.message_type = message_type
        self.rule = rule
        self.real = real
        self.outside = outside


# What a connection announced it can do (X-PerchTail-Agent-Features): the
# agent redacts on its host what it sends for a command with a `redact`
# block, and lists and extracts archives itself (#382, #249).
FEATURE_REDACTION = "redaction/1"
# The agent says where links lead: a listing carries each entry's real path
# when a link leads elsewhere, and a content command is read only when its
# path really is where the command expects (`expect_real`), refused with a
# "link_target" error otherwise (collectors/agent.py). Content is never read
# from an agent without it.
FEATURE_LINKS = "links/1"
# The agent says a content command is still running: asked with `progress`
# (only of a connection that announced it, since an older agent ignores the
# field and an older server would take the message for the answer), it sends
# a `progress` message every 10 seconds during which it sent nothing else,
# and each one counts as activity for that command's wait, as a chunk does.
# A member at the end of a large archive, redacted on the host, can take
# much longer than the wait to reach, and so can listing a large archive.
# An agent that announced it before list_archive sent progress ignores the
# field there and answers within the wait or not at all, as before.
FEATURE_PROGRESS = "progress/1"
# The commands that are asked for progress.
PROGRESS_COMMANDS = frozenset({"fetch", "read_range", "fetch_archive", "list_archive"})


class ArchiveSink(Protocol):
    """Where a fetch_archive's members go (#382, #249): `start_member` is
    called with each archive_member message, in order, and `write` with the
    content of the member last started. Either raises ValueError for
    something it won't accept, which fails the command."""

    def start_member(self, message: dict) -> None: ...

    def write(self, data: bytes) -> object: ...


@dataclass
class _Stream:
    """A fetch the agent sends in `fetch_chunk` messages (#205), written to
    `sink` as each chunk arrives so the file is never held in memory."""

    sink: BinaryIO | ArchiveSink
    limit: int | None
    # A fetch_archive (#382, #249): archive_member messages share `seq`
    # with the chunks, and every chunk belongs to the member last started.
    archive: bool = False
    member_started: bool = False
    next_seq: int = 0
    chunks: int = 0
    written: int = 0
    last_activity: float = field(default_factory=time.monotonic)


@dataclass
class _AgentConnection:
    websocket: WebSocket
    # What the agent announced on connect (X-PerchTail-Agent-Features).
    features: frozenset[str] = frozenset()
    # The address it connected from (app/request_context.py), if known.
    address: str | None = None
    # Whether falling back to redacting on the server was logged already:
    # once per connection (#382, #249).
    fallback_logged: bool = False
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    streams: dict[str, _Stream] = field(default_factory=dict)
    # When the agent last said a command asked for progress that isn't
    # streamed (a read_range) is still running (FEATURE_PROGRESS).
    progress: dict[str, float] = field(default_factory=dict)
    # When the agent last sent anything (#333), for the keepalive.
    last_message_at: float = field(default_factory=time.monotonic)


def _fail_pending(connection: _AgentConnection, reason: str) -> None:
    for future in connection.pending.values():
        if not future.done():
            future.set_exception(AgentNotConnectedError(reason))


async def _close(connection: _AgentConnection, reason: str) -> None:
    """On the event loop: fails what's waiting on the connection and closes
    its socket. 1008 (policy violation) because the agent's credentials or
    source no longer allow it; it reconnects and is then refused."""
    _fail_pending(connection, reason)
    try:
        await connection.websocket.close(code=1008, reason=reason)
    except Exception as exc:  # noqa: BLE001 - it may already be gone
        logger.debug("agent.server_close_failed", error=str(exc))


def disconnect_after_commit(session, source_id: int, reason: str) -> None:
    """Closes the source's agent connection once `session` commits, so the
    agent can't reconnect before the change that refuses it is written."""
    from sqlalchemy import event

    def _after_commit(_session) -> None:
        get_agent_registry().disconnect(source_id, reason)

    event.listen(session, "after_commit", _after_commit, once=True)


@dataclass(frozen=True)
class ConnectionReplaced:
    """A source's agent connection was replaced by one from another
    address (see AgentRegistry.register): two agents may be using the same
    token. Kept until the source's next connection that replaces none."""

    previous_address: str | None
    address: str | None
    at: datetime


class AgentRegistry:
    def __init__(self):
        self._connections: dict[int, _AgentConnection] = {}
        self._replaced: dict[int, ConnectionReplaced] = {}
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

    def features(self, source_id: int) -> frozenset[str]:
        """What the source's connected agent announced it can do; empty when
        none is connected."""
        with self._lock:
            connection = self._connections.get(source_id)
        return connection.features if connection is not None else frozenset()

    def redacts_on_host(self, source_id: int) -> bool:
        """Whether the source's connected agent redacts on its host
        (FEATURE_REDACTION), so content with redaction never leaves it
        unredacted (#382, #249)."""
        return FEATURE_REDACTION in self.features(source_id)

    def first_redaction_fallback(self, source_id: int) -> bool:
        """True the first time a connection's content is redacted on the
        server because its agent can't redact on the host, so that is
        logged once per connection, not on every request."""
        with self._lock:
            connection = self._connections.get(source_id)
            if connection is None or connection.fallback_logged:
                return False
            connection.fallback_logged = True
            return True

    def register(
        self,
        source_id: int,
        websocket: WebSocket,
        features: frozenset[str] = frozenset(),
        address: str | None = None,
    ) -> "_AgentConnection | None":
        """Makes `websocket` the source's connection, with the `features` its
        agent announced and the `address` it came from. If one was already
        registered (the agent reconnected before the old socket was noticed
        as dead, or a second agent is using the same token), it's replaced:
        its pending commands fail now instead of waiting out their timeout,
        and it is returned so the caller can close it (and audit it).

        A replacement from another address is kept as the source's
        `connection_replaced` warning until a connection that replaces
        nothing; a connection that replaces nothing clears it."""
        with self._lock:
            replaced = self._connections.get(source_id)
            self._connections[source_id] = _AgentConnection(
                websocket=websocket, features=frozenset(features), address=address
            )
            if replaced is None:
                self._replaced.pop(source_id, None)
            elif replaced.address != address:
                self._replaced[source_id] = ConnectionReplaced(
                    previous_address=replaced.address, address=address, at=utcnow()
                )
        if replaced is not None:
            _fail_pending(replaced, "agent connection replaced by a newer one")
            logger.warning(
                "agent.connection_replaced",
                source_id=source_id,
                previous_address=replaced.address,
                address=address,
            )
        logger.info("agent.connected", source_id=source_id)
        return replaced

    def connection_replaced(self, source_id: int) -> ConnectionReplaced | None:
        """The source's warning that its connection was replaced by one from
        another address, until its next connection that replaces none."""
        with self._lock:
            return self._replaced.get(source_id)

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

    def disconnect(self, source_id: int, reason: str) -> bool:
        """Closes the source's agent connection from the server side: its
        token was regenerated, or the source was disabled or deleted. The
        agent authenticated when it connected, so without this it would
        keep answering for the source with a token that no longer works.
        Callable from any thread; the source shows as disconnected at once,
        and the socket is closed on the event loop. True when there was a
        connection to close."""
        with self._lock:
            connection = self._connections.pop(source_id, None)
        if connection is None:
            return False
        logger.info("agent.disconnected_by_server", source_id=source_id, reason=reason)
        loop = self._loop
        if loop is None or loop.is_closed():
            _fail_pending(connection, reason)
            return True
        asyncio.run_coroutine_threadsafe(_close(connection, reason), loop)
        return True

    def is_current(self, source_id: int, websocket: WebSocket) -> bool:
        """Whether `websocket` is still the source's registered connection."""
        with self._lock:
            connection = self._connections.get(source_id)
        return connection is not None and connection.websocket is websocket

    def touch(self, source_id: int, websocket: WebSocket) -> None:
        """Records that the agent just sent something (#333)."""
        with self._lock:
            connection = self._connections.get(source_id)
        if connection is not None and connection.websocket is websocket:
            connection.last_message_at = time.monotonic()

    async def watch(
        self,
        source_id: int,
        websocket: WebSocket,
        *,
        interval: float = AGENT_PING_INTERVAL_SECONDS,
        dead_after: float = AGENT_DEAD_AFTER_SECONDS,
    ) -> bool:
        """The keepalive for one connection (#333), run alongside its receive
        loop: pings the agent every `interval` and returns True once nothing
        has come from it for `dead_after` -- the connection is then
        unregistered, so the source shows as disconnected, and the caller
        closes the socket. Returns False when the connection was replaced or
        unregistered in the meantime."""
        while True:
            await asyncio.sleep(interval)
            with self._lock:
                connection = self._connections.get(source_id)
            if connection is None or connection.websocket is not websocket:
                return False
            silent = time.monotonic() - connection.last_message_at
            if silent >= dead_after:
                logger.warning(
                    "agent.connection_dead", source_id=source_id, silent_seconds=round(silent)
                )
                self.unregister(source_id, websocket)
                return True
            try:
                await websocket.send_json({"type": "ping", "id": f"ping-{uuid.uuid4()}"})
            except Exception as exc:  # noqa: BLE001 - the connection may be gone
                logger.info("agent.ping_failed", source_id=source_id, error=str(exc))
                self.unregister(source_id, websocket)
                return True

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
        message_type = message.get("type", "")
        if isinstance(message_type, str) and message_type.endswith("_error"):
            code = message.get("code")
            real = message.get("real")
            limit = message.get("limit")
            future.set_exception(
                AgentCommandError(
                    str(message.get("error", "agent command failed")),
                    code=code if isinstance(code, str) else None,
                    size=as_int(message.get("size")),
                    message_type=message_type,
                    rule=as_int(message.get("rule")),
                    real=real if isinstance(real, str) else None,
                    outside=message.get("outside") is True,
                    limit=limit if isinstance(limit, str) else None,
                )
            )
            return
        stream = connection.streams.get(request_id)
        if stream is not None and message.get("streamed"):
            # `chunks` counts the fetch_chunk messages only, not a
            # fetch_archive's archive_member messages.
            complete = as_int(message.get("chunks")) == stream.chunks and (
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
        if stream.archive and not stream.member_started:
            future.set_exception(AgentError("agent sent archive content before its member"))
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
        stream.chunks += 1
        stream.last_activity = time.monotonic()

    async def receive_archive_member(
        self, source_id: int, message: dict, websocket: WebSocket | None = None
    ) -> None:
        """Called from the WS endpoint's receive loop for each
        `archive_member` of a fetch_archive (#382, #249): hands it to the
        command's sink, which starts the member's file. Numbered with the
        chunks; out of order, or for a command that isn't a fetch_archive,
        it fails the command."""
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
        if not stream.archive or as_int(message.get("seq")) != stream.next_seq:
            future.set_exception(AgentError("agent sent an archive member out of order"))
            return
        try:
            await asyncio.to_thread(stream.sink.start_member, message)  # type: ignore[union-attr]
        except (OSError, ValueError) as exc:
            if not future.done():
                future.set_exception(AgentError(f"agent sent an archive member refused: {exc}"))
            return
        stream.member_started = True
        stream.next_seq += 1
        stream.last_activity = time.monotonic()

    def receive_progress(
        self, source_id: int, message: dict, websocket: WebSocket | None = None
    ) -> None:
        """Called from the WS endpoint's receive loop for each `progress`
        message (FEATURE_PROGRESS): the command it names is still running,
        which counts as activity for its wait, as a chunk does. Ignored from
        a connection that didn't announce the feature or has since been
        replaced, and for a command nobody waits on any more."""
        with self._lock:
            connection = self._connections.get(source_id)
        if connection is None or FEATURE_PROGRESS not in connection.features:
            return
        if websocket is not None and connection.websocket is not websocket:
            return
        request_id = message.get("id", "")
        future = connection.pending.get(request_id)
        if future is None or future.done():
            return
        now = time.monotonic()
        stream = connection.streams.get(request_id)
        if stream is not None:
            stream.last_activity = now
        elif request_id in connection.progress:
            connection.progress[request_id] = now

    async def _send_command(
        self,
        source_id: int,
        command: dict,
        timeout: float,
        sink: BinaryIO | ArchiveSink | None = None,
        limit: int | None = None,
        archive: bool = False,
        require_feature: str | tuple[str, ...] | None = None,
        progress: bool = False,
    ) -> dict:
        with self._lock:
            connection = self._connections.get(source_id)
        if connection is None:
            raise AgentNotConnectedError(f"no agent connected for source {source_id}")
        required = (require_feature,) if isinstance(require_feature, str) else require_feature
        for feature in required or ():
            if feature in connection.features:
                continue
            # The agent reconnected without it since the caller checked: the
            # command is never sent, so an agent that would ignore it can't
            # answer with what it was meant to keep on its host. A missing
            # FEATURE_LINKS is coded apart: the agent must be updated.
            raise AgentCommandError(
                f"the agent doesn't support {feature}",
                code="links_missing" if feature == FEATURE_LINKS else "feature_missing",
                message_type="feature_error",
            )

        # Asked for progress only when this connection announced it: an
        # older agent would ignore the field and never send any.
        progress = progress and FEATURE_PROGRESS in connection.features
        if progress:
            command = {**command, "progress": True}
        request_id = command["id"]
        future: asyncio.Future = self._loop.create_future()
        connection.pending[request_id] = future
        stream = None
        completed = False
        if sink is not None:
            stream = _Stream(sink=sink, limit=limit, archive=archive)
            connection.streams[request_id] = stream
        elif progress:
            connection.progress[request_id] = time.monotonic()
        try:
            await connection.websocket.send_json(command)
            if stream is None and not progress:
                try:
                    return await asyncio.wait_for(future, timeout)
                except TimeoutError as exc:
                    raise AgentTimeoutError(
                        f"agent for source {source_id} did not respond in time"
                    ) from exc
            if stream is None:
                # Asked for progress: it times out only when neither the
                # answer nor a progress message has come for `timeout`.
                while True:
                    last = connection.progress.get(request_id, 0.0)
                    remaining = last + timeout - time.monotonic()
                    if remaining <= 0:
                        raise AgentTimeoutError(
                            f"agent for source {source_id} did not respond in time"
                        )
                    done, _ = await asyncio.wait({future}, timeout=remaining)
                    if done:
                        return future.result()
            # A streamed fetch of a large file can take much longer than
            # `timeout` in all; it only times out when no chunk (nor, with
            # FEATURE_PROGRESS, progress message) has arrived for that long.
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
            connection.progress.pop(request_id, None)
            if stream is not None and not completed:
                # Gave up (timeout, size limit, bad chunk): tell the agent
                # to stop reading the file. Best effort; an agent older
                # than 0.5.3 answers with an unknown_error nobody awaits.
                try:
                    await connection.websocket.send_json({"type": "cancel", "id": request_id})
                except Exception as exc:  # noqa: BLE001 - the connection may be gone
                    logger.debug("agent.cancel_not_sent", source_id=source_id, error=str(exc))

    def send_command_sync(
        self,
        source_id: int,
        command_type: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        sink: BinaryIO | ArchiveSink | None = None,
        limit: int | None = None,
        archive: bool = False,
        require_feature: str | tuple[str, ...] | None = None,
        command_id: str | None = None,
        **fields,
    ) -> dict:
        """The entry point collectors/agent.py actually calls — runs on
        whatever thread FastAPI's thread pool put the sync path operation
        on, schedules the real work onto the main event loop, and blocks
        this thread until it completes.

        With `sink`, `fetch_chunk` messages for this command are written to
        it as they arrive, refusing more than `limit` bytes (#205); the
        timeout then applies to the wait for each chunk, not to the whole
        transfer. With `archive` (a fetch_archive, #382, #249), `sink` is an
        ArchiveSink and also gets the archive_member messages, and `limit`
        counts the content of every member. With `require_feature` (one or
        several), the command is only sent to a connection that announced
        them (AgentCommandError coded "feature_missing" otherwise, or
        "links_missing" for FEATURE_LINKS). `command_id`
        sets the command's id, for a caller that tracks later messages about
        it (an upgrade's progress, #319).

        A content command (PROGRESS_COMMANDS) sent to an agent that
        announced FEATURE_PROGRESS asks it for progress: each progress
        message counts as activity, so the timeout applies to the wait for
        the answer, a chunk or a progress message, not to the whole
        command."""
        if self._loop is None:
            raise AgentNotConnectedError("agent registry has no event loop bound yet")

        progress = command_type in PROGRESS_COMMANDS and FEATURE_PROGRESS in self.features(
            source_id
        )
        command = {**fields, "type": command_type, "id": command_id or str(uuid.uuid4())}
        future = asyncio.run_coroutine_threadsafe(
            self._send_command(
                source_id,
                command,
                timeout,
                sink=sink,
                limit=limit,
                archive=archive,
                require_feature=require_feature,
                progress=progress,
            ),
            self._loop,
        )
        if sink is not None or progress:
            # No overall deadline: the in-loop wait times out when chunks
            # (or progress messages) stop.
            return future.result()
        # A few seconds of slack beyond the in-loop timeout so a slow
        # scheduler doesn't race a TimeoutError against this call's own —
        # the inner asyncio.wait_for is the real deadline.
        return future.result(timeout=timeout + 5)


@lru_cache
def get_agent_registry() -> AgentRegistry:
    return AgentRegistry()
