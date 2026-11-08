import hashlib
import os
import secrets
import socket
from collections import Counter
from datetime import timedelta
from pathlib import Path
from shutil import disk_usage

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, col, func, select

from app.agent_registry import get_agent_registry
from app.alerts import interval as alerts_interval
from app.alerts import last_pass as alerts_last_pass
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import AuditChainState, GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.config import get_settings
from app.db import get_session, sqlite_file
from app.health import uptime_seconds
from app.jobs import job_name, job_records
from app.log_rotation import retention_pressure
from app.logging_config import get_logger
from app.metrics import PROMETHEUS_CONTENT_TYPE, render_metrics
from app.models import Alert, AlertDelivery, MonitoringToken, Protocol, Source
from app.name_index import interval as name_index_interval
from app.scheduler import scheduler
from app.scratch import get_scratch_store
from app.source_status import FAILING
from app.timeutils import utcnow
from app.version import APP_VERSION

router = APIRouter(prefix="/monitoring", tags=["monitoring"])
# GET /metrics sits at the root, where Prometheus looks by default.
metrics_router = APIRouter(tags=["monitoring"])
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
    # The SQLite file and its write-ahead log (#346); None for another
    # database, or when the file can't be read.
    size_bytes: int | None = None
    wal_bytes: int | None = None


class ScratchHealth(BaseModel):
    used_bytes: int
    max_bytes: int
    used_fraction: float


class DiskHealth(BaseModel):
    """Free and total space of the filesystem holding a directory; None
    when it can't be read (the directory doesn't exist)."""

    free_bytes: int | None
    total_bytes: int | None
    free_fraction: float | None


class InstanceHealth(BaseModel):
    """Which process answered, for a setup with more than one."""

    hostname: str
    pid: int


class AgentHealth(BaseModel):
    configured: int
    connected: int


class NameIndexHealth(BaseModel):
    """The file-name index (#304) over enabled sources that take part in
    it. oldest_* is the source whose last pass (whatever its outcome) is
    the oldest; sources_with_message counts the ones whose last pass failed
    or stopped short."""

    enabled: bool
    sources: int
    never_indexed: int
    oldest_indexed_at: str | None
    oldest_age_seconds: float | None
    sources_with_message: int


class SourceChecksHealth(BaseModel):
    """Background source checks (#276, #343) over enabled sources."""

    failing: int
    backing_off: int


class AuditIntegrityHealth(BaseModel):
    """The last audit hash-chain verification (AuditChainState)."""

    status: str
    last_checked_at: str | None
    broken_row_id: int | None


class SchedulerJobHealth(BaseModel):
    id: str
    next_run_at: str | None
    # The job's stored record (#327); None until it has run once.
    name: str | None = None
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_success_at: str | None = None
    last_success_age_seconds: float | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None


class SchedulerHealth(BaseModel):
    running: bool
    # Jobs whose last run failed (#330).
    failed_jobs: int
    jobs: list[SchedulerJobHealth]


class AlertsHealth(BaseModel):
    """Live alerts (#305): the last pass, whether the next one is late,
    files that didn't fit in it and wait for the next ones, webhook calls
    waiting to be retried, and deliveries given up on (#328)."""

    last_pass_at: str | None
    last_pass_age_seconds: float | None
    overdue: bool
    files_waiting: int
    pending_deliveries: int
    failed_deliveries: int


class LogsHealth(BaseModel):
    used_bytes: int
    max_total_bytes: int
    retention_days: int
    # True once the LOG_MAX_TOTAL_MB limit has deleted logs that
    # LOG_RETENTION_DAYS would still have kept (#172): logging is too
    # verbose for the space it has. Doesn't change the overall status.
    size_limit_deleted_early: bool
    size_limit_deleted_files: int
    size_limit_newest_deleted_day: str | None


class DetailedHealth(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    instance: InstanceHealth
    database: DatabaseHealth
    scratch: ScratchHealth
    disks: dict[str, DiskHealth]
    sources_by_protocol: dict[str, int]
    agent: AgentHealth
    name_index: NameIndexHealth
    source_checks: SourceChecksHealth
    alerts: AlertsHealth
    scheduler: SchedulerHealth
    audit_integrity: AuditIntegrityHealth
    logs: LogsHealth


# Scratch use at or above this share of SCRATCH_MAX_GB makes the status
# degraded, and so does free space below DISK_FREE_MIN_FRACTION on the data,
# scratch or log filesystem.
SCRATCH_MAX_FRACTION = 0.9
DISK_FREE_MIN_FRACTION = 0.05


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
    return compute_health(session)


@metrics_router.get(
    "/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(_require_monitoring_bearer_token)],
)
def prometheus_metrics(session: Session = Depends(get_session)):
    """The same report in the Prometheus text format (#331)."""
    return PlainTextResponse(
        render_metrics(compute_health(session)), media_type=PROMETHEUS_CONTENT_TYPE
    )


def _iso(value):
    return value.isoformat() if value else None


def _age_seconds(value, now) -> float | None:
    return (now - value).total_seconds() if value else None


def compute_health(session: Session) -> DetailedHealth:
    """The one health computation behind /monitoring/health and /metrics."""
    settings = get_settings()
    now = utcnow()

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

    disks = {
        "data": _disk(settings.data_dir),
        "scratch": _disk(settings.scratch_dir),
        "logs": _disk(settings.log_dir),
    }
    disk_low = any(
        disk.free_fraction is not None and disk.free_fraction < DISK_FREE_MIN_FRACTION
        for disk in disks.values()
    )

    enabled_protocols = session.exec(select(Source.protocol).where(Source.enabled.is_(True))).all()
    protocol_counts = Counter(enabled_protocols)
    sources_by_protocol = {
        protocol.value: protocol_counts.get(protocol, 0) for protocol in Protocol
    }

    agent_configured = sources_by_protocol.get(Protocol.agent.value, 0)
    agent_connected = get_agent_registry().connected_count()

    last = alerts_last_pass()
    alerts_threshold = alerts_interval(session) * 2 + timedelta(seconds=30)
    if last["at"] is not None:
        sweep_overdue = now - last["at"] > alerts_threshold
    else:
        # Grace period since startup before the first pass has had a
        # chance to run at all.
        sweep_overdue = uptime_seconds() > alerts_threshold.total_seconds()
    alerts_health = AlertsHealth(
        last_pass_at=_iso(last["at"]),
        last_pass_age_seconds=_age_seconds(last["at"], now),
        overdue=sweep_overdue,
        files_waiting=last["stats"].files_waiting if last["stats"] else 0,
        pending_deliveries=session.exec(select(func.count()).select_from(AlertDelivery)).one(),
        failed_deliveries=session.exec(select(func.sum(Alert.failed_deliveries))).one() or 0,
    )

    records = job_records(session)
    jobs = []
    for job in scheduler.get_jobs():
        record = records.get(job.id)
        jobs.append(
            SchedulerJobHealth(
                id=job.id,
                next_run_at=_iso(job.next_run_time),
                name=job_name(job.id),
                last_started_at=_iso(record.last_started_at) if record else None,
                last_finished_at=_iso(record.last_finished_at) if record else None,
                last_success_at=_iso(record.last_success_at) if record else None,
                last_success_age_seconds=(
                    _age_seconds(record.last_success_at, now) if record else None
                ),
                last_ok=record.last_ok if record else None,
                last_error=record.last_error if record else None,
                last_duration_ms=record.last_duration_ms if record else None,
            )
        )
    scheduler_healthy = scheduler.running and all(job.next_run_at is not None for job in jobs)
    failed_jobs = sum(1 for job in jobs if job.last_ok is False)

    audit_integrity = _audit_integrity(session)

    overall = "ok"
    if not db_reachable:
        overall = "error"
    elif (
        scratch_fraction >= SCRATCH_MAX_FRACTION
        or not scheduler_healthy
        or sweep_overdue
        or failed_jobs > 0
        or disk_low
        or audit_integrity.status == "broken"
    ):
        overall = "degraded"
    elif agent_configured > 0 and agent_connected == 0:
        overall = "degraded"

    size_bytes, wal_bytes = _database_size()
    return DetailedHealth(
        status=overall,
        version=APP_VERSION,
        uptime_seconds=uptime_seconds(),
        instance=InstanceHealth(hostname=socket.gethostname(), pid=os.getpid()),
        database=DatabaseHealth(
            reachable=db_reachable,
            latency_ms=db_latency_ms,
            size_bytes=size_bytes,
            wal_bytes=wal_bytes,
        ),
        scratch=ScratchHealth(
            used_bytes=scratch_used, max_bytes=store.max_bytes, used_fraction=scratch_fraction
        ),
        disks=disks,
        sources_by_protocol=sources_by_protocol,
        agent=AgentHealth(configured=agent_configured, connected=agent_connected),
        name_index=_name_index_health(session, now),
        source_checks=_source_checks_health(session, now),
        alerts=alerts_health,
        scheduler=SchedulerHealth(running=scheduler.running, failed_jobs=failed_jobs, jobs=jobs),
        audit_integrity=audit_integrity,
        logs=_logs_health(settings),
    )


def _disk(path: str) -> DiskHealth:
    try:
        usage = disk_usage(path)
    except OSError:
        return DiskHealth(free_bytes=None, total_bytes=None, free_fraction=None)
    return DiskHealth(
        free_bytes=usage.free,
        total_bytes=usage.total,
        free_fraction=usage.free / usage.total if usage.total else None,
    )


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _database_size() -> tuple[int | None, int | None]:
    path = sqlite_file()
    if path is None:
        return None, None
    size = _file_size(path)
    if size is None:
        return None, None
    # No WAL file at all counts as an empty one (after a clean shutdown).
    return size, _file_size(path.with_name(path.name + "-wal")) or 0


def _name_index_health(session: Session, now) -> NameIndexHealth:
    taking_part = (Source.enabled.is_(True), Source.name_index_enabled.is_(True))

    def count(*conditions) -> int:
        return session.exec(
            select(func.count()).select_from(Source).where(*taking_part, *conditions)
        ).one()

    oldest = session.exec(select(func.min(Source.name_indexed_at)).where(*taking_part)).one()
    return NameIndexHealth(
        enabled=name_index_interval(session) is not None,
        sources=count(),
        never_indexed=count(col(Source.name_indexed_at).is_(None)),
        oldest_indexed_at=_iso(oldest),
        oldest_age_seconds=_age_seconds(oldest, now),
        sources_with_message=count(col(Source.name_index_message).is_not(None)),
    )


def _source_checks_health(session: Session, now) -> SourceChecksHealth:
    def count(condition) -> int:
        return session.exec(
            select(func.count()).select_from(Source).where(Source.enabled.is_(True), condition)
        ).one()

    return SourceChecksHealth(
        failing=count(Source.status == FAILING),
        backing_off=count(Source.check_backoff_until > now),
    )


def _audit_integrity(session: Session) -> AuditIntegrityHealth:
    state = session.exec(select(AuditChainState)).first()
    if state is None:
        return AuditIntegrityHealth(status="unknown", last_checked_at=None, broken_row_id=None)
    return AuditIntegrityHealth(
        status=state.status,
        last_checked_at=_iso(state.last_checked_at),
        broken_row_id=state.broken_row_id,
    )


def _logs_health(settings) -> LogsHealth:
    log_dir = Path(settings.log_dir)
    used = 0
    if log_dir.is_dir():
        used = sum(
            p.stat().st_size
            for p in log_dir.iterdir()
            if p.is_file() and p.name.startswith("perchtail.log")
        )
    pressure = retention_pressure()
    return LogsHealth(
        used_bytes=used,
        max_total_bytes=int(settings.log_max_total_mb * 1024 * 1024),
        retention_days=settings.log_retention_days,
        size_limit_deleted_early=pressure.deleted_files > 0,
        size_limit_deleted_files=pressure.deleted_files,
        size_limit_newest_deleted_day=pressure.newest_deleted_day,
    )
