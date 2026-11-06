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
  default, 0 = off), stalest first, one at a time. Push-agent sources are
  skipped: their status is the live connection.

Only the public message (SourceError.public_message) is stored; the raw
error stays in the server log, as for the source page's "Check"."""

import threading
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
from app.logging_config import get_logger
from app.models import Protocol, Source
from app.timeutils import utcnow

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


def check_interval(session: Session) -> timedelta | None:
    minutes = system_settings.get_int(session, "source_check_minutes")
    return timedelta(minutes=minutes) if minutes > 0 else None


def run_source_checks() -> None:
    """The background check, run every minute by APScheduler: checks the
    sources whose status is older than the interval, stalest first."""
    from app.collectors.registry import CONNECTORS
    from app.db import engine

    with Session(engine) as session:
        interval = check_interval(session)
        if interval is None:
            return
        cutoff = utcnow() - interval
        due = session.exec(
            select(Source)
            .where(
                Source.enabled.is_(True),
                Source.protocol != Protocol.agent,
                (col(Source.status_checked_at).is_(None)) | (Source.status_checked_at < cutoff),
            )
            .order_by(col(Source.status_checked_at).is_(None).desc(), Source.status_checked_at)
        ).all()
        for source in due:
            session.expunge(source)
    for source in due:
        connector = CONNECTORS.get(source.protocol)
        if connector is None:
            continue
        failures = check(source, connector)
        if failures:
            # The connectivity failure, if any, is the useful reason.
            root, exc = next(((r, e) for r, e in failures if is_connectivity_error(e)), failures[0])
            record(source.id, False, failure_message(exc))
        else:
            record(source.id, True)


def forget_cache() -> None:
    """For tests."""
    with _lock:
        _last_written.clear()
