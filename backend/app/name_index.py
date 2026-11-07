"""The file-name index (#304): which rule-visible files each source has,
with their size and modification time -- never their content. It lets the
explorer search file names across every source without listing every server
on every search, and lets alerts see which files grew.

A background pass runs every minute and lists the sources whose index is
older than the interval (Settings -> System, 15 minutes by default),
stalest first, a few at a time. Each listing walks the source recursively
through its rules, like the explorer does, down to MAX_DEPTH levels (a guard
against links that loop back up). Files that are new or changed are written;
files that are gone are removed only when the listing reached every folder,
so a folder that couldn't be read doesn't empty its part of the index.

Storage and search are in app/name_index_store.py."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, update
from sqlmodel import Session, col, select

from app import source_roots, system_settings
from app.collectors import local as local_collector
from app.collectors.base import DirEntry
from app.logging_config import get_logger
from app.models import FileNameEntry, Protocol, Rule, Source
from app.source_status import failure_message
from app.timeutils import utcnow

logger = get_logger(__name__)

MAX_DEPTH = 32
# Sources listed at once by the background pass.
WORKERS = 4

_running: set[int] = set()
_running_lock = threading.Lock()


@dataclass
class NameIndexResult:
    files: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    complete: bool = True
    message: str | None = None


def interval(session: Session) -> timedelta | None:
    """How old a source's index may get, or None when the index is off."""
    if not system_settings.get_bool(session, "name_index_enabled"):
        return None
    return timedelta(minutes=system_settings.get_int(session, "name_index_minutes"))


def _local_allowed(source: Source) -> bool:
    if source.protocol != Protocol.local or source.is_system:
        return True
    paths = [source.base_path] + [r["path"] for r in source_roots.roots_of(source)]
    return all(local_collector.base_path_allowed(p) for p in paths)


def _walk(
    connector, source: Source, rules: list[Rule]
) -> tuple[dict[str, DirEntry], NameIndexResult]:
    """Every rule-visible file of the source, by path. A failure listing the
    top level is a failed pass (nothing is changed); a failure below it, or
    a folder deeper than MAX_DEPTH, makes the pass incomplete."""
    result = NameIndexResult()
    files: dict[str, DirEntry] = {}
    pending: list[tuple[str, int]] = [("", 0)]
    unreadable = too_deep = 0
    while pending:
        directory, depth = pending.pop()
        try:
            entries = connector.list_directory(source, rules, directory)
        except Exception as exc:  # noqa: BLE001 - reported on the source
            if directory == "":
                result.complete = False
                result.message = failure_message(exc)
                return {}, result
            unreadable += 1
            logger.info(
                "name_index.folder_unreadable", source_id=source.id, path=directory, error=str(exc)
            )
            continue
        for entry in entries:
            if entry.is_dir:
                if depth + 1 >= MAX_DEPTH:
                    too_deep += 1
                else:
                    pending.append((entry.path, depth + 1))
            else:
                files[entry.path] = entry
    if unreadable or too_deep:
        result.complete = False
        parts = []
        if unreadable:
            parts.append(
                f"{unreadable} {'folder' if unreadable == 1 else 'folders'} couldn't be listed"
            )
        if too_deep:
            folders = "folder" if too_deep == 1 else "folders"
            parts.append(f"not listed below {MAX_DEPTH} levels ({too_deep} {folders})")
        result.message = "; ".join(parts)
    result.files = len(files)
    return files, result


def _store(
    session: Session, source_id: int, files: dict[str, DirEntry], result: NameIndexResult
) -> None:
    now = utcnow()
    known = {
        path: (entry_id, size, mtime)
        for entry_id, path, size, mtime in session.exec(
            select(
                FileNameEntry.id, FileNameEntry.path, FileNameEntry.size, FileNameEntry.mtime
            ).where(FileNameEntry.source_id == source_id)
        ).all()
    }
    new = [
        FileNameEntry(source_id=source_id, path=path, size=e.size, mtime=e.mtime, seen_at=now)
        for path, e in files.items()
        if path not in known
    ]
    session.add_all(new)
    result.added = len(new)
    for path, entry in files.items():
        old = known.get(path)
        if old and (old[1] != entry.size or old[2] != entry.mtime):
            session.execute(
                update(FileNameEntry)
                .where(FileNameEntry.id == old[0])
                .values(size=entry.size, mtime=entry.mtime, seen_at=now)
            )
            result.changed += 1
    if result.complete:
        gone = [entry_id for path, (entry_id, _s, _m) in known.items() if path not in files]
        for start in range(0, len(gone), 500):
            session.execute(
                delete(FileNameEntry).where(col(FileNameEntry.id).in_(gone[start : start + 500]))
            )
        result.removed = len(gone)


def index_source_names(source_id: int, engine=None) -> NameIndexResult | None:
    """Lists one source and updates its part of the index. None when it was
    skipped: unknown, disabled, opted out, already being indexed, or a
    protocol without a connector."""
    from app.collectors.registry import CONNECTORS
    from app.db import engine as default_engine

    engine = engine or default_engine
    with _running_lock:
        if source_id in _running:
            return None
        _running.add(source_id)
    try:
        with Session(engine) as session:
            source = session.get(Source, source_id)
            if source is None or not source.enabled or not source.name_index_enabled:
                return None
            connector = CONNECTORS.get(source.protocol)
            if connector is None:
                return None
            rules = list(session.exec(select(Rule).where(Rule.source_id == source_id)).all())
            for rule in rules:
                session.expunge(rule)
            session.expunge(source)

        if _local_allowed(source):
            files, result = _walk(connector, source, rules)
        else:
            files, result = {}, NameIndexResult(
                complete=False, message="Outside the server's allowed directories"
            )

        with Session(engine) as session:
            if result.message is None or files:
                _store(session, source_id, files, result)
            row = session.get(Source, source_id)
            if row is not None:
                row.name_indexed_at = utcnow()
                row.name_index_message = result.message
                if files or result.complete:
                    row.name_index_files = result.files
                session.add(row)
            session.commit()
        logger.info(
            "name_index.source_done",
            source_id=source_id,
            files=result.files,
            added=result.added,
            changed=result.changed,
            removed=result.removed,
            complete=result.complete,
        )
        return result
    except Exception as exc:  # noqa: BLE001 - one source never stops the pass
        logger.warning("name_index.source_failed", source_id=source_id, error=str(exc))
        return None
    finally:
        with _running_lock:
            _running.discard(source_id)


def due_source_ids(session: Session) -> list[int]:
    every = interval(session)
    if every is None:
        return []
    cutoff = utcnow() - every
    return list(
        session.exec(
            select(Source.id)
            .where(
                Source.enabled.is_(True),
                Source.name_index_enabled.is_(True),
                (col(Source.name_indexed_at).is_(None)) | (Source.name_indexed_at < cutoff),
            )
            .order_by(col(Source.name_indexed_at).is_(None).desc(), Source.name_indexed_at)
        ).all()
    )


def run_name_index_sweep(engine=None) -> None:
    """The background pass (every minute): indexes the sources that are due,
    WORKERS at a time."""
    from app.db import engine as default_engine

    engine = engine or default_engine
    with Session(engine) as session:
        due = due_source_ids(session)
    if not due:
        return
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="name-index") as pool:
        list(pool.map(lambda source_id: index_source_names(source_id, engine), due))


def drop_source_names(session: Session, source_id: int) -> None:
    """Forgets a source's files (the caller commits): when it's deleted, its
    index is turned off, or where it reads from changes."""
    session.execute(delete(FileNameEntry).where(FileNameEntry.source_id == source_id))
    source = session.get(Source, source_id)
    if source is not None:
        source.name_indexed_at = None
        source.name_index_files = None
        source.name_index_message = None
        session.add(source)


def index_in_background(source_id: int) -> bool:
    """ "Index now": starts a pass for one source in a thread. False when
    one is already running for it."""
    with _running_lock:
        if source_id in _running:
            return False
    threading.Thread(
        target=index_source_names, args=(source_id,), name=f"name-index-{source_id}", daemon=True
    ).start()
    return True
