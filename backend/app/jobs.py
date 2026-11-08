"""Scheduled jobs with a record (#327) and a bounded shutdown (#335).

Every job is added through `register`, which gives it a stable id and name
and wraps it so each run stores its start, finish, result and error in
job_run. At startup a job whose interval has gone by since its last
successful run (or that never ran) runs CATCH_UP_DELAY after startup,
instead of a full interval later: a container restarted more often than
the audit purge's or integrity check's interval used to never run them.

The stored error is the exception's type and message with URLs and
credential-looking values blanked, cut to MAX_ERROR characters."""

import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.logging_config import get_logger
from app.models import JobRun
from app.timeutils import utcnow

logger = get_logger(__name__)

MAX_ERROR = 500
# Long enough for startup to settle, short enough to count as "soon".
CATCH_UP_DELAY = timedelta(seconds=30)

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_SECRET = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|key)\s*[=:]\s*\S+")

# job id -> display name, for the records of jobs registered in this process.
_names: dict[str, str] = {}


def error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = _URL.sub("<url>", text)
    text = _SECRET.sub(lambda m: f"{m.group(1)}=<hidden>", text)
    return text[:MAX_ERROR]


def _engine():
    from app.db import engine  # deferred, so tests can patch app.db.engine

    return engine


def _write(job_id: str, **values) -> None:
    """Best effort: a record that can't be saved never fails the job."""
    try:
        with Session(_engine()) as session:
            row = session.get(JobRun, job_id) or JobRun(job_id=job_id)
            for name, value in values.items():
                setattr(row, name, value)
            session.add(row)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("job.record_not_saved", job_id=job_id, error=str(exc))


def run_recorded(job_id: str, func: Callable, kwargs: dict | None = None) -> bool:
    """Runs one job and records it. Never raises; True when it worked."""
    started = utcnow()
    clock = time.monotonic()
    _write(job_id, last_started_at=started)
    ok, error = True, None
    try:
        func(**(kwargs or {}))
    except Exception as exc:  # noqa: BLE001 - recorded and logged here
        ok, error = False, error_text(exc)
        logger.exception("job.failed", job_id=job_id)
    finished = utcnow()
    values = {
        "last_finished_at": finished,
        "last_ok": ok,
        "last_error": error,
        "last_duration_ms": int((time.monotonic() - clock) * 1000),
    }
    if ok:
        values["last_success_at"] = finished
    _write(job_id, **values)
    return ok


def is_overdue(job_id: str, interval: timedelta, now: datetime | None = None) -> bool:
    """True when the job never succeeded or its last success is over an
    interval old. False when the record can't be read (it runs on schedule)."""
    try:
        with Session(_engine()) as session:
            row = session.get(JobRun, job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("job.record_not_read", job_id=job_id, error=str(exc))
        return False
    if row is None or row.last_success_at is None:
        return True
    return (now or utcnow()) - row.last_success_at >= interval


def register(
    scheduler,
    job_id: str,
    name: str,
    func: Callable,
    interval: timedelta,
    *,
    catch_up: bool = False,
    kwargs: dict | None = None,
):
    """Adds `func` as an interval job with a stable id. With catch_up, an
    overdue job's first run is CATCH_UP_DELAY from now."""
    _names[job_id] = name
    extra = {}
    if catch_up and is_overdue(job_id, interval):
        extra["next_run_time"] = datetime.now(UTC) + CATCH_UP_DELAY
        logger.info("job.catch_up", job_id=job_id)
    return scheduler.add_job(
        run_recorded,
        "interval",
        seconds=interval.total_seconds(),
        args=[job_id, func, kwargs],
        id=job_id,
        name=name,
        replace_existing=True,
        **extra,
    )


def job_records(session: Session) -> dict[str, JobRun]:
    """Every stored job record, by job id (for /monitoring/health)."""
    return {row.job_id: row for row in session.exec(select(JobRun)).all()}


def job_name(job_id: str) -> str | None:
    return _names.get(job_id)


def shutdown(scheduler, pools, timeout: float) -> bool:
    """Stops the scheduler and drains `pools`, all within `timeout` seconds
    in total. Running work gets to finish inside the limit; anything still
    running after it is left behind. True when everything finished."""
    deadline = time.monotonic() + timeout
    finished = True
    if scheduler.running:
        # shutdown(wait=True) has no limit of its own.
        stopper = threading.Thread(
            target=scheduler.shutdown, kwargs={"wait": True}, name="scheduler-stop", daemon=True
        )
        stopper.start()
        stopper.join(max(deadline - time.monotonic(), 0))
        if stopper.is_alive():
            finished = False
            logger.warning("shutdown.jobs_still_running")
    for pool in pools:
        if not pool.drain(deadline - time.monotonic()):
            finished = False
            logger.warning("shutdown.pool_still_running", pool=pool.name)
    logger.info("shutdown.background_work_stopped", finished=finished)
    return finished
