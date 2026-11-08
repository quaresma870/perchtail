"""Source reachability (#276): each source's last known status, kept on the
server so every user sees the same thing.

- `record` is called by the connector wrapper (collectors/errors.py) after
  every real operation -- a browse, an open, a Follow poll, the search
  indexer -- and by the background check. A success means "ok"; only
  errors about reaching the source (unreachable, credentials, host key,
  TLS) mean "failing". An error about one file (not found, permission,
  locked) says nothing about the source and changes nothing.
- Writes go through their own short session, so a failing request never
  takes the status update down with it, and are throttled: the same
  status is written at most once a minute per source, so a Follow poll
  every second doesn't write every second.
- `run_source_checks` is the APScheduler job: every minute it checks the
  sources whose status is older than the System interval (10 minutes by
  default, 0 = off), stalest first, WORKERS at a time (#343). A check that
  runs past check_deadline() is given up on and counts as failing, so one
  stalled source only holds its own worker. Push-agent sources are
  skipped: their status is the live connection.
- After FAILURES_BEFORE_BACKOFF failed background checks in a row, a
  source's background checks (and its file-name index passes) back off for
  a growing, jittered time capped at BACKOFF_CAP; it stays "failing" until
  any operation on it works. Kept on the source row, so a restart doesn't
  reset it. Browsing it is never blocked by this.

Only the public message (SourceError.public_message) is stored; the raw
error stays in the server log, as for the source page's "Check"."""

import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, wait
from datetime import timedelta

from sqlmodel import Session, col, select

from app import source_roots, system_settings
from app.collectors.base import (
    SourceAuthError,
    SourceError,
    SourceHostKeyError,
    SourceTLSError,
    SourceUnreachableError,
)
from app.config import get_settings
from app.logging_config import get_logger
from app.models import Protocol, Source
from app.timeutils import utcnow
from app.worker_pool import DrainablePool

logger = get_logger(__name__)

OK = "ok"
FAILING = "failing"
# Errors that mean "can't use this source at all", as opposed to one file.
_CONNECTIVITY = (SourceUnreachableError, SourceAuthError, SourceHostKeyError, SourceTLSError)
_REWRITE_AFTER = timedelta(minutes=1)
_MAX_MESSAGE = 300

# source id -> (status, message, when last written), so repeated identical
# results don't each hit the database.
_last_written: dict[int, tuple[str, str | None, object]] = {}
_lock = threading.Lock()
# Background checks run at once; the pool outlives each pass and is drained
# at shutdown (app/jobs.py).
WORKERS = 4
FAILURES_BEFORE_BACKOFF = 3
BACKOFF_CAP = timedelta(minutes=30)
TIMED_OUT = "The source didn't answer in time"
_pool = DrainablePool(WORKERS, "source-check")
_pass_lock = threading.Lock()
# Sources whose check is still running, possibly past its deadline, so a
# stalled one isn't handed another worker by the next pass.
_in_flight: set[int] = set()
# Off in the test suite (tests/conftest.py), which doesn't point app.db.engine
# at its database; the tests of this module turn it back on.
ENABLED = True


def is_connectivity_error(exc: BaseException) -> bool:
    return isinstance(exc, _CONNECTIVITY)


def failure_message(exc: BaseException) -> str:
    if isinstance(exc, SourceError):
        return exc.public_message
    return "The connection check failed"


def record(source_id: int | None, ok: bool, message: str | None = None) -> None:
    """Stores the status if it changed, or if the same one is over a minute
    old. Never raises: a status that can't be saved is only logged."""
    if source_id is None or not ENABLED:
        return
    status = OK if ok else FAILING
    message = None if ok else (message or "")[:_MAX_MESSAGE]
    now = utcnow()
    with _lock:
        previous = _last_written.get(source_id)
        if previous and previous[:2] == (status, message) and now - previous[2] < _REWRITE_AFTER:
            return
        _last_written[source_id] = (status, message, now)
    from app.db import engine  # deferred, so tests can patch app.db.engine

    try:
        with Session(engine) as session:
            source = session.get(Source, source_id)
            if source is None:
                return
            if source.status != status:
                log = logger.info if ok else logger.warning
                log("source.status_changed", source_id=source_id, status=status, reason=message)
            source.status = status
            source.status_message = message
            source.status_checked_at = now
            if ok:  # any success ends a backoff
                source.check_failures = 0
                source.check_backoff_until = None
            session.add(source)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - status is best-effort
        logger.warning("source.status_not_saved", source_id=source_id, error=str(exc))
        with _lock:
            _last_written.pop(source_id, None)


def check(source: Source, connector) -> list[tuple[dict, Exception]]:
    """Lists the base path of each of the source's folders, returning the
    failures (an empty list = reachable). The source page's "Check" and the
    background check both use it."""
    roots = source_roots.roots_of(source) or [{"label": None, "path": source.base_path}]
    failures = []
    for root in roots:
        target = source_roots.with_base_path(source, root["path"])
        try:
            connector.list_directory(target, [], "")
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            logger.warning(
                "source.check.failed", source_id=source.id, root=root["label"], error=str(exc)
            )
            failures.append((root, exc))
    return failures


def record_check(source_id: int | None, failures: list[tuple[dict, Exception]]) -> None:
    """The status a check found: failing only when it couldn't reach the
    source. A folder of a multi-root source that's missing is a setup problem
    the check reports, but the source answers, so it stays ok -- otherwise
    it would flip with every browse of another folder."""
    reaching = [exc for _root, exc in failures if is_connectivity_error(exc)]
    if reaching:
        record(source_id, False, failure_message(reaching[0]))
    else:
        record(source_id, True)


def check_interval(session: Session) -> timedelta | None:
    minutes = system_settings.get_int(session, "source_check_minutes")
    return timedelta(minutes=minutes) if minutes > 0 else None


def check_deadline() -> float:
    """Seconds a background check may take before it's given up on: a
    connect plus a stalled read, with room to spare."""
    return get_settings().remote_read_timeout_seconds * 2 + 10


def backoff_delay(failures: int, interval: timedelta) -> timedelta:
    """Doubles from twice the check interval with each failure past the
    threshold, capped, minus up to a quarter as jitter so sources that
    failed together don't come back together."""
    steps = max(failures - FAILURES_BEFORE_BACKOFF, 0)
    delay = min(interval * 2 ** (steps + 1), BACKOFF_CAP)
    # Spreading retries out, not a secret: the plain generator is fine.
    return delay * random.uniform(0.75, 1.0)  # nosec B311


def _note_failure(source_id: int, interval: timedelta) -> None:
    if not ENABLED:
        return
    from app.db import engine  # deferred, so tests can patch app.db.engine

    try:
        with Session(engine) as session:
            source = session.get(Source, source_id)
            if source is None:
                return
            source.check_failures = (source.check_failures or 0) + 1
            if source.check_failures >= FAILURES_BEFORE_BACKOFF:
                delay = backoff_delay(source.check_failures, interval)
                source.check_backoff_until = utcnow() + delay
                logger.warning(
                    "source.check.backing_off",
                    source_id=source_id,
                    failures=source.check_failures,
                    seconds=int(delay.total_seconds()),
                )
            session.add(source)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - best effort, like record()
        logger.warning("source.backoff_not_saved", source_id=source_id, error=str(exc))


def record_background_check(
    source_id: int | None, failures: list[tuple[dict, Exception]], interval: timedelta
) -> None:
    """record_check, plus the backoff count when it couldn't reach the
    source (a success resets it in record())."""
    record_check(source_id, failures)
    if source_id is not None and any(is_connectivity_error(exc) for _r, exc in failures):
        _note_failure(source_id, interval)


class _Task:
    def __init__(self, source: Source):
        self.source = source
        self.started: float | None = None
        self.finished = False
        self.abandoned = False


def _run_check(task: _Task, connector, interval: timedelta) -> None:
    task.started = time.monotonic()
    try:
        failures = check(task.source, connector)
    finally:
        with _lock:
            _in_flight.discard(task.source.id)
            task.finished = True
            abandoned = task.abandoned
    if not abandoned:
        record_background_check(task.source.id, failures, interval)


def _give_up(task: _Task, interval: timedelta) -> None:
    with _lock:
        if task.finished:
            return
        task.abandoned = True
    logger.warning("source.check.timed_out", source_id=task.source.id)
    record(task.source.id, False, TIMED_OUT)
    _note_failure(task.source.id, interval)


def run_source_checks() -> None:
    """The background check, run every minute by APScheduler: checks the
    sources whose status is older than the interval, stalest first, in the
    pool. Returns once each has finished or run past its deadline; a pass
    already running makes this one a no-op."""
    if not _pass_lock.acquire(blocking=False):
        return
    try:
        _run_pass()
    finally:
        _pass_lock.release()


def _run_pass() -> None:
    from app.collectors.registry import CONNECTORS
    from app.db import engine

    with Session(engine) as session:
        interval = check_interval(session)
        if interval is None:
            return
        now = utcnow()
        cutoff = now - interval
        due = session.exec(
            select(Source)
            .where(
                Source.enabled.is_(True),
                Source.protocol != Protocol.agent,
                (col(Source.status_checked_at).is_(None)) | (Source.status_checked_at < cutoff),
                (col(Source.check_backoff_until).is_(None)) | (Source.check_backoff_until <= now),
            )
            .order_by(col(Source.status_checked_at).is_(None).desc(), Source.status_checked_at)
        ).all()
        for source in due:
            session.expunge(source)

    running = []
    for source in due:
        connector = CONNECTORS.get(source.protocol)
        if connector is None:
            continue
        with _lock:
            if source.id in _in_flight:
                continue
            _in_flight.add(source.id)
        task = _Task(source)
        future = _pool.submit(_run_check, task, connector, interval)
        if future is None:  # shutting down
            with _lock:
                _in_flight.discard(source.id)
            break
        running.append((task, future))

    deadline = check_deadline()
    while running:
        wait([future for _t, future in running], timeout=0.25, return_when=FIRST_COMPLETED)
        clock = time.monotonic()
        still = []
        for task, future in running:
            if future.cancelled():  # dropped by a shutdown before it started
                with _lock:
                    _in_flight.discard(task.source.id)
            elif future.done():
                continue
            elif task.started is not None and clock - task.started > deadline:
                _give_up(task, interval)
            else:
                still.append((task, future))
        running = still


def pool() -> DrainablePool:
    """For the shutdown drain (app/main.py)."""
    return _pool


def forget(source_id: int) -> None:
    """Drops what was last written for a source, so its next result is
    written at once (after its connection settings change)."""
    with _lock:
        _last_written.pop(source_id, None)


def forget_cache() -> None:
    """For tests."""
    with _lock:
        _last_written.clear()
        _in_flight.clear()
