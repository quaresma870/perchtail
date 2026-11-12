import asyncio
import hashlib
import re

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app import agent_registry as registry_module
from app.agent_registry import AgentRegistry, get_agent_registry
from app.agent_upgrades import agent_connected, binary_path, on_agent_message
from app.db import get_session
from app.logging_config import get_logger
from app.models import Protocol, Source
from app.timeutils import utcnow

logger = get_logger(__name__)

router = APIRouter(tags=["agent"])


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer(value: str) -> str:
    return value[7:] if value.lower().startswith("bearer ") else ""


def _agent_source(session: Session, token: str) -> Source | None:
    """The enabled agent source `token` belongs to, or None."""
    if not token:
        return None
    source = session.exec(
        select(Source).where(
            Source.protocol == Protocol.agent, Source.agent_token_hash == _hash_token(token)
        )
    ).first()
    return source if source is not None and source.enabled else None


AGENT_OS_HEADER = "x-perchtail-agent-os"
_AGENT_OS_RE = re.compile(r"^[a-z0-9]{1,16}$")


def _reported_os(websocket: WebSocket) -> str | None:
    """The agent's OS from its connect header (#189), or None when absent
    (an agent older than 0.5.1) or not a plain GOOS name."""
    value = websocket.headers.get(AGENT_OS_HEADER, "").strip().lower()
    return value if _AGENT_OS_RE.match(value) else None


AGENT_VERSION_HEADER = "x-perchtail-agent-version"
_AGENT_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")


def _reported_version(websocket: WebSocket) -> str | None:
    """The agent's version from its connect header (#215), or None when
    absent (an agent older than 0.6) or not a plain version string."""
    value = websocket.headers.get(AGENT_VERSION_HEADER, "").strip()
    return value if _AGENT_VERSION_RE.match(value) else None


@router.websocket("/agent/connect")
async def agent_connect(websocket: WebSocket, session: Session = Depends(get_session)):
    """The push-agent's persistent connection (Protocol.agent — see
    app/models.py and app/agent_registry.py). Auth is a bearer token
    generated via POST /sources/{id}/agent-token, matched against its
    stored hash — there's no admin session/cookie involved since this is a
    machine-to-machine connection from a server that may have no browser at
    all."""
    token = _bearer(websocket.headers.get("authorization", ""))
    source = _agent_source(session, token)
    if source is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    registry = get_agent_registry()
    replaced = registry.register(source.id, websocket)
    if replaced is not None:
        # Close the superseded socket so a half-dead connection doesn't
        # linger; its own receive loop then ends, and its unregister below
        # is a no-op because it is no longer the registered connection.
        try:
            await replaced.websocket.close(
                code=status.WS_1000_NORMAL_CLOSURE,
                reason="replaced by a newer connection for this source",
            )
        except Exception as exc:  # noqa: BLE001 - it may already be gone
            logger.debug("agent.replaced_close_failed", source_id=source.id, error=str(exc))
    source.agent_last_seen_at = utcnow()
    agent_os = _reported_os(websocket)
    if agent_os is not None and agent_os != source.agent_os:
        logger.info(
            "agent.os_reported", source_id=source.id, agent_os=agent_os, previous=source.agent_os
        )
        source.agent_os = agent_os
    # Unlike the OS, a missing version is recorded too: an agent replaced by
    # one older than 0.6 mustn't keep showing the newer version.
    agent_version = _reported_version(websocket)
    if agent_version != source.agent_version:
        logger.info(
            "agent.version_reported",
            source_id=source.id,
            agent_version=agent_version,
            previous=source.agent_version,
        )
        source.agent_version = agent_version
    source_id = source.id
    bind = session.get_bind()
    session.add(source)
    # An upgrade from the portal (#319) ends with the agent coming back:
    # with the new version, it worked. Committed with the version.
    try:
        agent_connected(session, source_id, agent_version)
    except Exception as exc:  # noqa: BLE001 - never refuse the agent over it
        logger.error("agent.upgrade_update_failed", source_id=source_id, error=str(exc))
    session.commit()
    # The socket stays open for as long as the agent runs, and so does this
    # request's session. The commit handed its database connection back to
    # the pool; reading `source` again would expire-reload it and keep a
    # connection checked out until the agent disconnects, so a few dozen
    # agents would hold the whole pool and every other request would wait.
    # Only source_id is used from here on.
    del source

    # The token was checked before accept() and register(). A token
    # regenerated, or the source disabled or deleted, in between closed
    # whatever was registered then -- not this socket, which registered
    # after. Check again now that it's registered: a change committed from
    # here on finds it and closes it (registry.disconnect).
    current = session.exec(
        select(Source.agent_token_hash, Source.enabled).where(Source.id == source_id)
    ).first()
    session.rollback()  # end the read transaction; see above
    if current is None or current[0] != _hash_token(token) or not current[1]:
        registry.unregister(source_id, websocket)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # The receive loop and the keepalive (#333) run side by side: when the
    # agent goes silent the keepalive ends first, and the socket is closed
    # here rather than left to a TCP timeout that may never come.
    receiving = asyncio.create_task(_receive(websocket, registry, bind, source_id))
    watching = asyncio.create_task(
        registry.watch(
            source_id,
            websocket,
            interval=registry_module.AGENT_PING_INTERVAL_SECONDS,
            dead_after=registry_module.AGENT_DEAD_AFTER_SECONDS,
        )
    )
    try:
        done, _ = await asyncio.wait({receiving, watching}, return_when=asyncio.FIRST_COMPLETED)
        if receiving in done:
            receiving.result()  # anything but a disconnect is raised as before
        elif watching.result():
            receiving.cancel()
            try:
                await asyncio.wait_for(
                    websocket.close(
                        code=status.WS_1001_GOING_AWAY, reason="no answer from the agent"
                    ),
                    timeout=5,
                )
            except Exception as exc:  # noqa: BLE001 - the connection is dead anyway
                logger.debug("agent.dead_close_failed", source_id=source_id, error=str(exc))
    finally:
        receiving.cancel()
        watching.cancel()
        registry.unregister(source_id, websocket)


async def _receive(websocket: WebSocket, registry: AgentRegistry, bind, source_id: int) -> None:
    try:
        while True:
            message = await websocket.receive_json()
            registry.touch(source_id, websocket)
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "fetch_chunk":
                # Awaited before reading on, so the file is written as it
                # arrives and a slow disk slows the agent down (#205).
                await registry.receive_chunk(source_id, message, websocket)
            elif kind in ("upgrade_progress", "upgrade_failed"):
                # An accepted upgrade's progress (#319), from the current
                # connection only, written to its attempt off the loop.
                if registry.is_current(source_id, websocket):
                    await asyncio.to_thread(on_agent_message, bind, source_id, message)
            elif kind == "pong":
                pass  # the keepalive only needs to know something arrived
            else:
                registry.resolve_response(source_id, message, websocket)
    except WebSocketDisconnect:
        pass


@router.get("/agent/binaries/{name}")
def agent_binary(name: str, request: Request, session: Session = Depends(get_session)):
    """The agent binaries bundled with this server (#319), for agents that
    upgrade from the portal on a host without internet access: the
    release's SHA256SUMS, its signature, and the binaries of this server's
    version, nothing else. Only with an enabled agent source's token. The
    agent checks the signature and the hash itself, so the files are
    served as they are."""
    if _agent_source(session, _bearer(request.headers.get("authorization", ""))) is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authorized")
    path = binary_path(name)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)
