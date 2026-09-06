import hashlib

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
    registry.register(source.id, websocket)
    source.agent_last_seen_at = utcnow()
    session.add(source)
    session.commit()

    try:
        while True:
            message = await websocket.receive_json()
            registry.resolve_response(source.id, message)
    except WebSocketDisconnect:
        pass
    finally:
        registry.unregister(source.id)
