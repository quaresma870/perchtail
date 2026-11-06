import hashlib
import re

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from sqlmodel import Session, select

from app.agent_registry import get_agent_registry
from app.db import get_session
from app.logging_config import get_logger
from app.models import Protocol, Source
from app.timeutils import utcnow

logger = get_logger(__name__)

router = APIRouter(tags=["agent"])


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
    auth_header = websocket.headers.get("authorization", "")
    token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    source = session.exec(
        select(Source).where(
            Source.protocol == Protocol.agent, Source.agent_token_hash == _hash_token(token)
        )
    ).first()
    if source is None or not source.enabled:
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
    session.add(source)
    session.commit()

    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "fetch_chunk":
                # Awaited before reading on, so the file is written as it
                # arrives and a slow disk slows the agent down (#205).
                await registry.receive_chunk(source.id, message, websocket)
            else:
                registry.resolve_response(source.id, message, websocket)
    except WebSocketDisconnect:
        pass
    finally:
        registry.unregister(source.id, websocket)
