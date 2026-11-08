"""Live alerts (#305): a webhook is called when lines matching an alert's
query are written to the files it watches.

A pass runs every `alerts_interval_seconds` (Settings -> System, 60 s by
default). For each enabled alert whose owner may still alert, it resolves
the alert's scope against the owner's current access (enabled sources
only), takes the files the file-name index lists there that match the
alert's glob (never compressed files or archives), and reads from each
only what was appended since the last pass -- the same ranged read Tail
uses, so a file's content is never fetched whole or kept. A file first
seen starts at its end; a rotated or rewritten one (it shrank, or its
first bytes changed) starts again from the beginning.

Each source gets at most FILES_PER_SOURCE files and SOURCE_BUDGET_SECONDS
per alert per pass; the files checked least recently go first, so the
rest come round on the next passes, and the backlog shows in monitoring.

Matches are sent as one webhook call per alert per pass (#328): sent
right away, retried with backoff while it fails for up to an hour
(AlertDelivery), then dropped and counted on the alert."""

import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from sqlmodel import Session, col, delete, select

from app import system_settings
from app.archives import is_archive, is_transparent_gzip
from app.auth.models import Capability, GlobalCapability, User
from app.auth.rbac import visible_source_ids
from app.logging_config import get_logger
from app.models import (
    Alert,
    AlertDelivery,
    AlertFileState,
    FileNameEntry,
    Folder,
    Rule,
    Source,
)
from app.rules import compile_linear, glob_matches, is_visible, rules_case_insensitive
from app.timeutils import utcnow
from app.webhook_safety import UnsafeWebhookURLError, resolve_pinned_webhook_target

logger = get_logger(__name__)

FILES_PER_SOURCE = 20
SOURCE_BUDGET_SECONDS = 15.0
# At most this much is read from one file per pass; the rest is read on the
# next pass.
READ_BYTES = 1024 * 1024
# The start of a file that's hashed to notice rotation (same as Tail).
HEAD_BYTES = 64
MAX_LINE_CHARS = 2000
MAX_HITS_PER_DELIVERY = 50
RETRY_FOR = timedelta(hours=1)
_WEBHOOK_TIMEOUT_SECONDS = 10.0


@dataclass
class Hit:
    source_id: int
    source_name: str
    file_path: str
    offset: int
    line: str


@dataclass
class PassStats:
    alerts: int = 0
    files_read: int = 0
    files_waiting: int = 0
    hits: int = 0
    errors: list[str] = field(default_factory=list)


# What the last pass found, for /monitoring/health.
_last_pass: dict = {"at": None, "stats": None}
_pass_lock = threading.Lock()


def last_pass() -> dict:
    return dict(_last_pass)


def head_hash(start: bytes, offset: int) -> str:
    return hashlib.sha256(start[: min(HEAD_BYTES, offset)]).hexdigest()[:32]


def compile_query(query: str, regex: bool):
    """A callable telling whether a line matches: plain text anywhere in
    the line ignoring case, or an RE2 regex (linear time). Raises
    re2.error for a regex RE2 can't run."""
    if regex:
        pattern = compile_linear(query, ignore_case=True)
        return lambda line: pattern.search(line) is not None
    needle = query.casefold()
    return lambda line: needle in line.casefold()


def owner_may_alert(owner: User | None) -> bool:
    """Alerts send log lines out, so they only run while their owner is
    active and still holds manage_alerts, checked at every pass."""
    if owner is None or not owner.active:
        return False
    return owner.role.is_super_admin or GlobalCapability.manage_alerts in (
        owner.role.global_capabilities
    )


def folder_subtree(session: Session, folder_id: int) -> set[int]:
    ids = {folder_id}
    pending = [folder_id]
    while pending:
        children = session.exec(
            select(Folder.id).where(col(Folder.parent_folder_id).in_(pending))
        ).all()
        pending = [c for c in children if c not in ids]
        ids.update(pending)
    return ids


def scope_source_ids(session: Session, owner: User, alert: Alert) -> set[int]:
    """The enabled sources this alert may read now: its scope, within what
    the owner can view."""
    query = select(Source.id).where(Source.enabled.is_(True), Source.is_system.is_(False))
    if alert.source_id is not None:
        query = query.where(Source.id == alert.source_id)
    elif alert.folder_id is not None:
        query = query.where(col(Source.folder_id).in_(folder_subtree(session, alert.folder_id)))
    elif alert.customer_id is not None:
        query = query.where(Source.customer_id == alert.customer_id)
    ids = set(session.exec(query).all())
    visible = visible_source_ids(session, owner, Capability.view)
    return ids if visible is None else ids & set(visible)


def _watchable(path: str, glob: str, rules: list[Rule], case_insensitive: bool) -> bool:
    name = path.rsplit("/", 1)[-1]
    if is_transparent_gzip(name) or is_archive(name):
        return False
    if not glob_matches(glob, path, ignore_case=case_insensitive):
        return False
    return is_visible(path, rules, case_insensitive=case_insensitive)


def _complete_lines(data: bytes, at_limit: bool) -> bytes:
    """Only whole lines are matched; a last line still being written is
    read again on the next pass. When the read hit its size limit with no
    newline at all, it's taken as is so the alert doesn't stall on it."""
    end = data.rfind(b"\n")
    if end < 0:
        return data if at_limit else b""
    return data[: end + 1]


def _read_file(connector, source: Source, state: AlertFileState, matches, stats: PassStats):
    """Reads what was appended to one file since `state` and returns the
    matching lines; moves `state` on."""
    size, first, data = connector.read_range(
        source, state.file_path, state.offset, READ_BYTES, HEAD_BYTES
    )
    if state.offset > size or (
        state.head is not None and state.offset > 0 and head_hash(first, state.offset) != state.head
    ):
        # Rotated or rewritten: everything in it is new.
        state.offset = 0
        size, first, data = connector.read_range(source, state.file_path, 0, READ_BYTES, HEAD_BYTES)
    chunk = _complete_lines(data, at_limit=len(data) >= READ_BYTES)
    hits: list[Hit] = []
    position = state.offset
    for raw in chunk.split(b"\n")[:-1] if chunk.endswith(b"\n") else [chunk]:
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if line and matches(line):
            hits.append(
                Hit(source.id, source.name, state.file_path, position, line[:MAX_LINE_CHARS])
            )
        position += len(raw) + 1
    state.offset += len(chunk)
    state.head = head_hash(first, state.offset) if state.offset else None
    stats.files_read += 1
    return hits


def _current_sizes(
    connector, source: Source, rules: list[Rule], states: list[AlertFileState], started: float
) -> tuple[dict[str, int], set[str]]:
    """The size of each watched file now, from one listing per folder they
    sit in. A file whose folder couldn't be listed (an error, or the time
    budget ran out) is returned in the second set, to be read directly."""
    folders: dict[str, list[str]] = {}
    for state in states:
        folder = state.file_path.rsplit("/", 1)[0] if "/" in state.file_path else ""
        folders.setdefault(folder, []).append(state.file_path)
    sizes: dict[str, int] = {}
    unlisted: set[str] = set()
    for folder, paths in folders.items():
        if time.monotonic() - started > SOURCE_BUDGET_SECONDS:
            unlisted.update(paths)
            continue
        try:
            for entry in connector.list_directory(source, rules, folder):
                if not entry.is_dir:
                    sizes[entry.path] = entry.size
        except Exception as exc:  # noqa: BLE001 - its files are read directly instead
            logger.info(
                "alert.folder_list_failed", source_id=source.id, path=folder, error=str(exc)
            )
            unlisted.update(paths)
    return sizes, unlisted


def check_alert(session: Session, alert: Alert, stats: PassStats | None = None) -> list[Hit]:
    """One alert's pass: the new matching lines in its files. Updates and
    commits the per-file positions as it goes, source by source."""
    from app.collectors.registry import CONNECTORS

    stats = stats or PassStats()
    owner = session.get(User, alert.user_id)
    if not owner_may_alert(owner):
        return []
    matches = compile_query(alert.query, alert.regex)
    source_ids = scope_source_ids(session, owner, alert)
    if not source_ids:
        return []
    now = utcnow()
    states = {
        (s.source_id, s.file_path): s
        for s in session.exec(
            select(AlertFileState).where(
                AlertFileState.alert_id == alert.id,
                col(AlertFileState.source_id).in_(source_ids),
            )
        ).all()
    }
    hits: list[Hit] = []
    for source in session.exec(select(Source).where(col(Source.id).in_(source_ids))).all():
        connector = CONNECTORS.get(source.protocol)
        if connector is None:
            continue
        rules = list(session.exec(select(Rule).where(Rule.source_id == source.id)).all())
        case_insensitive = rules_case_insensitive(source)
        entries = session.exec(
            select(FileNameEntry.path, FileNameEntry.size).where(
                FileNameEntry.source_id == source.id
            )
        ).all()
        files = [
            (path, size)
            for path, size in entries
            if _watchable(path, alert.file_glob, rules, case_insensitive)
        ]
        known = []
        for path, size in files:
            state = states.get((source.id, path))
            if state is None:
                # First seen: start at its end, as listed.
                session.add(
                    AlertFileState(
                        alert_id=alert.id,
                        source_id=source.id,
                        file_path=path,
                        offset=size,
                        checked_at=None,
                    )
                )
            else:
                known.append(state)
        # Never hold SQLite's write lock across remote reads: a connector
        # records the source's status from this same thread, and would wait
        # on this session's lock until the busy timeout.
        session.commit()
        started = time.monotonic()
        # Current sizes come from listing each folder once (one connection
        # per folder), so only files whose size changed are read -- a read
        # is a connection of its own on SSH, SMB and WinRM.
        sizes, unlisted = _current_sizes(connector, source, rules, known, started)
        changed = [
            s for s in known if s.file_path in unlisted or sizes.get(s.file_path) != s.offset
        ]
        changed_paths = {s.file_path for s in changed}
        for state in known:
            if state.file_path not in changed_paths:
                state.checked_at = now
                session.add(state)
        changed.sort(key=lambda s: s.checked_at or now - timedelta(days=3650))
        for index, state in enumerate(changed):
            if index >= FILES_PER_SOURCE or time.monotonic() - started > SOURCE_BUDGET_SECONDS:
                stats.files_waiting += len(changed) - index
                break
            try:
                hits.extend(_read_file(connector, source, state, matches, stats))
            except Exception as exc:  # noqa: BLE001 - one file never stops the pass
                logger.info(
                    "alert.file_read_failed",
                    alert_id=alert.id,
                    source_id=source.id,
                    path=state.file_path,
                    error=str(exc),
                )
            state.checked_at = now
            session.add(state)
        session.commit()
    return hits


def _payload(alert: Alert, hits: list[Hit]) -> dict:
    return {
        "alert_id": alert.id,
        "alert_name": alert.name,
        "query": alert.query,
        "regex": alert.regex,
        "matched_count": len(hits),
        "hits": [
            {
                "source_id": hit.source_id,
                "source_name": hit.source_name,
                "file_path": hit.file_path,
                "offset": hit.offset,
                "line": hit.line,
            }
            for hit in hits[:MAX_HITS_PER_DELIVERY]
        ],
    }


def post_webhook(url: str, payload: dict) -> str | None:
    """Sends one webhook call; None when it worked, else why it didn't.
    The target is resolved and validated here, and pinned to the address
    that validation saw (see app/webhook_safety.py: no DNS rebinding)."""
    try:
        target = resolve_pinned_webhook_target(url)
        with httpx.Client(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = client.post(
                target.url,
                json=payload,
                headers={"Host": target.host_header},
                extensions={"sni_hostname": target.sni_hostname},
            )
        response.raise_for_status()
        return None
    except UnsafeWebhookURLError as exc:
        return f"blocked: {exc}"
    except httpx.HTTPError as exc:
        return str(exc) or exc.__class__.__name__


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(15, 2 ** max(0, attempts - 1)))


def deliver_due(session: Session) -> None:
    """Sends (again) every delivery that's due. One older than RETRY_FOR is
    dropped and counted on its alert."""
    now = utcnow()
    for delivery in session.exec(
        select(AlertDelivery).where(AlertDelivery.next_attempt_at <= now)
    ).all():
        alert = session.get(Alert, delivery.alert_id)
        if alert is None:
            session.delete(delivery)
            continue
        error = post_webhook(alert.webhook_url, delivery.payload)
        delivery.attempts += 1
        if error is None:
            logger.info("alert.webhook_sent", alert_id=alert.id, attempts=delivery.attempts)
            alert.last_fired_at = now
            alert.last_error = None
            session.delete(delivery)
        elif now - delivery.created_at >= RETRY_FOR:
            logger.warning("alert.webhook_given_up", alert_id=alert.id, error=error)
            alert.failed_deliveries += 1
            alert.last_error = error
            session.delete(delivery)
        else:
            logger.warning("alert.webhook_failed", alert_id=alert.id, error=error)
            alert.last_error = error
            delivery.next_attempt_at = now + _backoff(delivery.attempts)
            session.add(delivery)
        session.add(alert)
        session.commit()


def queue_delivery(session: Session, alert: Alert, hits: list[Hit]) -> None:
    now = utcnow()
    session.add(
        AlertDelivery(
            alert_id=alert.id, payload=_payload(alert, hits), created_at=now, next_attempt_at=now
        )
    )


def interval(session: Session) -> timedelta:
    return timedelta(seconds=system_settings.get_int(session, "alerts_interval_seconds"))


def run_alert_pass(engine=None, *, force: bool = False) -> PassStats | None:
    """The scheduled pass (every 15 s, doing work once the configured
    interval has gone by). None when it wasn't due or another is running."""
    from app.db import engine as default_engine

    engine = engine or default_engine
    if not _pass_lock.acquire(blocking=False):
        return None
    try:
        with Session(engine) as session:
            if not force and _last_pass["at"] is not None:
                if utcnow() - _last_pass["at"] < interval(session):
                    return None
            stats = PassStats()
            alerts = session.exec(select(Alert).where(Alert.enabled.is_(True))).all()
            for alert in alerts:
                try:
                    hits = check_alert(session, alert, stats)
                    stats.alerts += 1
                    alert.last_checked_at = utcnow()
                    session.add(alert)
                    if hits:
                        stats.hits += len(hits)
                        queue_delivery(session, alert, hits)
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - one alert never stops the others
                    logger.exception("alert.evaluate_failed", alert_id=alert.id)
                    stats.errors.append(f"alert {alert.id}: {exc}")
                    session.rollback()
            deliver_due(session)
            _last_pass["at"] = utcnow()
            _last_pass["stats"] = stats
            return stats
    finally:
        _pass_lock.release()


def forget_alert(session: Session, alert_id: int) -> None:
    """Drops an alert's per-file positions and pending deliveries (the
    caller deletes the alert and commits)."""
    session.execute(delete(AlertFileState).where(AlertFileState.alert_id == alert_id))
    session.execute(delete(AlertDelivery).where(AlertDelivery.alert_id == alert_id))


def drop_scoped_alerts(session: Session, **scope: int) -> None:
    """Deletes the alerts scoped to a source, folder or customer that is
    being deleted, with their positions and deliveries (the caller
    commits). Ids are never reused, but an alert pointing at nothing would
    just sit there doing nothing."""
    ((key, value),) = scope.items()
    for alert in session.exec(select(Alert).where(getattr(Alert, key) == value)).all():
        forget_alert(session, alert.id)
        session.delete(alert)


def forget_source(session: Session, source_id: int) -> None:
    """Drops every alert's positions in a source that's deleted or now
    reads somewhere else (the caller commits)."""
    session.execute(delete(AlertFileState).where(AlertFileState.source_id == source_id))
