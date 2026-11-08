"""In-process operational state for the detailed health endpoint
(app/api/monitoring.py). Deliberately in-memory, not persisted to SQLite —
same "per-process, doesn't need to survive a restart" reasoning
app.agent_registry already uses for live connection state: uptime and
"last successful sweep" are about *this running process*, not durable
business data."""

import time

_started_at: float | None = None


def mark_started() -> None:
    global _started_at
    _started_at = time.monotonic()


def uptime_seconds() -> float:
    if _started_at is None:
        return 0.0
    return time.monotonic() - _started_at


# How long the readiness probe's database write waits for the write lock,
# instead of the normal busy timeout (30 s): a probe gives up much sooner.
READY_DB_TIMEOUT_MS = 2000


def _database_writable(engine) -> bool:
    """A real write that's rolled back (#330): it needs SQLite's write lock,
    so a locked or read-only database fails it, and leaves nothing behind."""
    from sqlalchemy import insert

    from app.logging_config import get_logger
    from app.models import JobRun

    try:
        with engine.connect() as connection:
            sqlite = engine.dialect.name == "sqlite"
            if sqlite:
                previous = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
                connection.exec_driver_sql(f"PRAGMA busy_timeout={READY_DB_TIMEOUT_MS}")
            try:
                connection.execute(insert(JobRun.__table__).values(job_id="__readyz__"))
                connection.rollback()
            finally:
                if sqlite:
                    connection.exec_driver_sql(f"PRAGMA busy_timeout={int(previous)}")
        return True
    except Exception as exc:  # noqa: BLE001 - reported as not ready
        get_logger(__name__).warning("readyz.database_not_writable", error=str(exc))
        return False


def _directory_writable(path) -> bool:
    """Creates, writes and removes a small file in `path`: a full disk or a
    read-only mount fails it."""
    import tempfile

    from app.logging_config import get_logger

    try:
        with tempfile.TemporaryFile(dir=path, prefix=".readyz-") as probe:
            probe.write(b"ok")
            probe.flush()
        return True
    except OSError as exc:
        get_logger(__name__).warning("readyz.data_dir_not_writable", error=str(exc))
        return False


def readiness_failures(engine, data_dir, scheduler) -> list[str]:
    """What keeps this instance from taking traffic (GET /readyz): an empty
    list when it's ready."""
    failures = []
    if not _database_writable(engine):
        failures.append("database")
    if not _directory_writable(data_dir):
        failures.append("data_dir")
    if not scheduler.running:
        failures.append("scheduler")
    return failures
