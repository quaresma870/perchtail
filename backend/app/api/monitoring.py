import hashlib
import secrets
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, select

from app.agent_registry import get_agent_registry
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.config import get_settings
from app.db import get_session
from app.health import last_search_sweep_at, uptime_seconds
from app.logging_config import get_logger
from app.models import MonitoringToken, Protocol, Source
from app.scheduler import scheduler
from app.scratch import get_scratch_store
from app.timeutils import utcnow
from app.version import APP_VERSION

router = APIRouter(prefix="/monitoring", tags=["monitoring"])
logger = get_logger(__name__)

require_manage = require_global_capability(
    GlobalCapability.manage_system_settings, get_current_active_user
)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MonitoringTokenResult(BaseModel):
    token: str


class MonitoringTokenStatus(BaseModel):
    configured: bool


@router.post("/token", response_model=MonitoringTokenResult)
def regenerate_monitoring_token(
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Issues a fresh bearer token gating GET /monitoring/health, for an
    external monitoring system (Zabbix, Prometheus) that can't do an
    interactive cookie-session login. Only the hash is stored -- same
    "hash at rest, plaintext shown once" pattern as
    Source.agent_token_hash (see api/sources.py's regenerate_agent_token).
    Singleton by convention: this replaces whatever row (and therefore
    token) already existed, so regenerating invalidates the previous one,
    same UX as resetting a user's password."""
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)

    existing = session.exec(select(MonitoringToken)).first()
    if existing is not None:
        existing.token_hash = token_hash
        existing.created_at = utcnow()
        session.add(existing)
    else:
        session.add(MonitoringToken(token_hash=token_hash, created_at=utcnow()))

    record_audit_event(
        session,
        user_id=user.id,
        action="monitoring_token.regenerate",
        target_type="monitoring_token",
    )
    session.commit()
    return MonitoringTokenResult(token=token)


@router.get("/token", response_model=MonitoringTokenStatus)
def monitoring_token_status(
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    row = session.exec(select(MonitoringToken)).first()
    return MonitoringTokenStatus(configured=row is not None)


def _require_monitoring_bearer_token(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> None:
    """Separate from user-session auth entirely -- see MonitoringToken's
    docstring in models.py. Deliberately not require_global_capability:
    a monitoring system authenticates with this one deployment-wide
    credential, not a user account."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()

    row = session.exec(select(MonitoringToken)).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Monitoring token not configured",
        )
    if not secrets.compare_digest(_hash_token(token), row.token_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


class DatabaseHealth(BaseModel):
    reachable: bool
    latency_ms: float | None


class ScratchHealth(BaseModel):
    used_bytes: int
    max_bytes: int
    used_fraction: float


class AgentHealth(BaseModel):
    configured: int
    connected: int


class SchedulerJobHealth(BaseModel):
    id: str
    next_run_at: str | None


class SchedulerHealth(BaseModel):
    running: bool
    jobs: list[SchedulerJobHealth]


class SearchIndexHealth(BaseModel):
    last_sweep_at: str | None
    overdue: bool


class DetailedHealth(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    database: DatabaseHealth
    scratch: ScratchHealth
    sources_by_protocol: dict[str, int]
    agent: AgentHealth
    search_index: SearchIndexHealth
    scheduler: SchedulerHealth


@router.get(
    "/health",
    response_model=DetailedHealth,
    dependencies=[Depends(_require_monitoring_bearer_token)],
)
def detailed_health(session: Session = Depends(get_session)):
    """The endpoint external monitoring (Zabbix HTTP agent item + JSONPath
    preprocessing per metric -- see docs/monitoring.md) actually polls.
    Deliberately separate from GET /healthz, which stays a fast,
    unauthenticated liveness check for the Docker healthcheck/
    orchestrator -- this one is slower (a real DB round-trip) and carries
    the structured data worth alerting on."""
    settings = get_settings()

    db_reachable = True
    db_latency_ms: float | None = None
    try:
        start = utcnow()
        session.execute(text("SELECT 1"))
        db_latency_ms = (utcnow() - start).total_seconds() * 1000
    except Exception:
        logger.exception("monitoring.health.db_check_failed")
        db_reachable = False

    store = get_scratch_store()
    scratch_used = store.total_bytes()
    scratch_fraction = scratch_used / store.max_bytes if store.max_bytes else 0.0

    enabled_protocols = session.exec(select(Source.protocol).where(Source.enabled.is_(True))).all()
    protocol_counts = Counter(enabled_protocols)
    sources_by_protocol = {
        protocol.value: protocol_counts.get(protocol, 0) for protocol in Protocol
    }

    agent_configured = sources_by_protocol.get(Protocol.agent.value, 0)
    agent_connected = get_agent_registry().connected_count()

    last_sweep_iso = last_search_sweep_at()
    sweep_overdue_threshold = timedelta(seconds=settings.search_index_interval_seconds * 2)
    if last_sweep_iso is not None:
        sweep_overdue = utcnow() - datetime.fromisoformat(last_sweep_iso) > sweep_overdue_threshold
    else:
        # Grace period since startup before the first sweep has had a
        # chance to run at all.
        sweep_overdue = uptime_seconds() > sweep_overdue_threshold.total_seconds()

    jobs = [
        SchedulerJobHealth(
            id=job.id,
            next_run_at=job.next_run_time.isoformat() if job.next_run_time else None,
        )
        for job in scheduler.get_jobs()
    ]
    scheduler_healthy = scheduler.running and all(job.next_run_at is not None for job in jobs)

    overall = "ok"
    if not db_reachable:
        overall = "error"
    elif scratch_fraction >= 0.9 or not scheduler_healthy or sweep_overdue:
        overall = "degraded"
    elif agent_configured > 0 and agent_connected == 0:
        overall = "degraded"

    return DetailedHealth(
        status=overall,
        version=APP_VERSION,
        uptime_seconds=uptime_seconds(),
        database=DatabaseHealth(reachable=db_reachable, latency_ms=db_latency_ms),
        scratch=ScratchHealth(
            used_bytes=scratch_used, max_bytes=store.max_bytes, used_fraction=scratch_fraction
        ),
        sources_by_protocol=sources_by_protocol,
        agent=AgentHealth(configured=agent_configured, connected=agent_connected),
        search_index=SearchIndexHealth(last_sweep_at=last_sweep_iso, overdue=sweep_overdue),
        scheduler=SchedulerHealth(running=scheduler.running, jobs=jobs),
    )
