"""Phase 3 full-text search (see ROADMAP.md) — a background indexer that
walks each opted-in source's rule-visible files and stores short, per-line
snippets in a SQLite FTS5 virtual table (schema created by
app.db.ensure_search_schema), plus the search query itself.

This is a deliberately separate, lagging, approximate secondary index, not
a cache or a mirror: CLAUDE.md's live-browsing/ephemeral-fetch model still
governs every actual file open or download, unaffected by anything here.
Indexing is opt-in per source (Source.search_indexing_enabled, default
False) since it's the one place in this project that stores a form of log
content at rest, even if only short line-level snippets rather than full
files."""

import html
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import InvalidRequestError
from sqlmodel import Session, select

from app.archives import (
    DecompressionLimitError,
    decompress_gzip,
    is_archive,
    is_transparent_gzip,
)
from app.collectors.base import (
    DirEntry,
    FetchTooLargeError,
    PathOutsideBaseError,
    SourceNotFoundError,
    SourcePermissionError,
)
from app.collectors.registry import CONNECTORS
from app.config import get_settings
from app.health import record_search_sweep
from app.logging_config import get_logger
from app.models import AlertFileState, Rule, SearchIndexState, Source
from app.timeutils import utcnow

logger = get_logger(__name__)

_CONNECTORS = CONNECTORS

# Enough to reliably catch binary content without reading an entire large
# file just to decide whether to skip it.
_BINARY_SNIFF_BYTES = 8192
# Guards against a single pathological line (e.g. a minified blob or a
# corrupt file with no newlines) bloating the index or the search results.
_MAX_SNIPPET_CHARS = 500


@dataclass
class IndexStats:
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    # Files that couldn't be read this sweep (no permission, gone since the
    # listing, grown past the limit); also counted in `skipped` (#204).
    unreadable: int = 0
    # Changed files left for the next sweep because the source ran out of
    # its time budget (#218).
    deferred: int = 0


@dataclass
class SearchHit:
    source_id: int
    file_path: str
    line_number: int
    snippet_html: str = field(repr=False)
    # "content" -- the query matched this line's text, snippet_html
    # highlights the match in the line itself (the original, only kind of
    # hit before path matching was added). "path" -- the query matched the
    # file's path/name, not this particular line's content; snippet_html
    # highlights the match in the path instead, and line_number is just
    # some line from that file (see search()'s dedup, not a claim that this
    # specific line is where the match is).
    matched_field: str = "content"


def _iter_files(connector, source: Source, rules: list[Rule], directory: str = ""):
    """Recursively walks a source's rule-visible files, same recursion shape
    as api/archive.py's _zip_directory. Files are already filtered by
    is_visible inside each connector's list_directory; directories are
    always listed (never filtered), same convention as everywhere else."""
    for entry in connector.list_directory(source, rules, directory):
        if entry.is_dir:
            yield from _iter_files(connector, source, rules, entry.path)
        else:
            yield entry


def _read_text(connector, source: Source, entry: DirEntry, max_bytes: int) -> str | None:
    """Returns entry's decoded text content, or None if it should be skipped
    (an archive container, too large, or binary). Transparent .gz files are
    decompressed first, same as the viewer does on open — worth the extra
    step here too, since rotated logs spend most of their life gzipped."""
    if is_archive(entry.name):
        # v1 scope: index plain files and transparent .gz only, not the
        # members of .zip/.tar.gz containers — a real design question of
        # its own (index every member? how deep?), left for a future pass.
        return None
    if entry.size > max_bytes:
        return None

    with connector.local_copy(source, entry.path) as local_path:
        if is_transparent_gzip(entry.name):
            with tempfile.TemporaryDirectory() as tmp:
                decompressed = Path(tmp) / "decompressed"
                try:
                    decompress_gzip(local_path, decompressed, max_bytes)
                except (OSError, DecompressionLimitError):
                    return None
                if decompressed.stat().st_size > max_bytes:
                    return None
                data = decompressed.read_bytes()
        else:
            data = local_path.read_bytes()

    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", errors="replace")


def _delete_fts_rows(session: Session, source_id: int, file_path: str) -> None:
    session.execute(
        text(
            "DELETE FROM search_index_fts WHERE source_id = :source_id AND file_path = :file_path"
        ),
        {"source_id": source_id, "file_path": file_path},
    )


def _insert_fts_rows(session: Session, source_id: int, file_path: str, content: str) -> None:
    rows = [
        {
            "source_id": source_id,
            "file_path": file_path,
            "line_number": line_number,
            "snippet": line[:_MAX_SNIPPET_CHARS],
        }
        for line_number, line in enumerate(content.splitlines(), start=1)
        if line.strip()
    ]
    if rows:
        session.execute(
            text(
                "INSERT INTO search_index_fts (source_id, file_path, line_number, snippet) "
                "VALUES (:source_id, :file_path, :line_number, :snippet)"
            ),
            rows,
        )


def _delete_alert_file_states(session: Session, source_id: int, file_path: str | None = None):
    query = select(AlertFileState).where(AlertFileState.source_id == source_id)
    if file_path is not None:
        query = query.where(AlertFileState.file_path == file_path)
    for state in session.exec(query).all():
        session.delete(state)


def delete_source_index(session: Session, source_id: int) -> None:
    """Drops everything indexed for a source -- its FTS rows and per-file
    state. Doesn't commit."""
    session.execute(
        text("DELETE FROM search_index_fts WHERE source_id = :source_id"),
        {"source_id": source_id},
    )
    for state in session.exec(
        select(SearchIndexState).where(SearchIndexState.source_id == source_id)
    ).all():
        session.delete(state)
    _delete_alert_file_states(session, source_id)


_UNREADABLE = (SourcePermissionError, SourceNotFoundError, PathOutsideBaseError, FetchTooLargeError)


def index_source(session: Session, source: Source, deadline: float | None = None) -> IndexStats:
    """Indexes (or re-indexes) every rule-visible file in `source`. Safe to
    call repeatedly — unchanged files (by size, see SearchIndexState's
    docstring on why size alone) are skipped, and files no longer present or
    no longer rule-visible are removed from the index.

    Each file is committed on its own, and its content is fetched from the
    source before anything is written (#188). A first sweep of a large
    source takes minutes; holding one write transaction open across all of
    it locked SQLite and every login or audit write failed meanwhile. Now
    the write lock is held only while one file's rows are written.

    Stops early, leaving what's indexed so far, if the source is disabled,
    opted out or deleted while it runs (rows of a source that's no longer
    indexed are purged by purge_inactive_source_indexes on the next sweep).

    `deadline`, a time.monotonic() value, bounds how long this call spends
    reading files (#218): past it, the remaining changed files are left for
    the next sweep (counted in IndexStats.deferred), so one large source
    can't hold the sweep for every other source."""
    connector = _CONNECTORS.get(source.protocol)
    if connector is None:
        return IndexStats()

    settings = get_settings()
    max_bytes = int(settings.search_index_max_file_size_mb * 1024 * 1024)
    source_id = source.id
    rules = list(session.exec(select(Rule).where(Rule.source_id == source_id)).all())

    stats = IndexStats()
    # (id, size, indexed_at) rather than the rows themselves: every commit
    # below expires loaded objects, and reloading each one to compare sizes
    # would cost a query per unchanged file.
    known = {
        state.file_path: (state.id, state.size, state.indexed_at)
        for state in session.exec(
            select(SearchIndexState).where(SearchIndexState.source_id == source_id)
        ).all()
    }
    session.commit()  # end the read transaction before the slow part

    entries = list(_iter_files(connector, source, rules))
    seen_paths = {entry.path for entry in entries}
    pending = [
        entry for entry in entries if entry.path not in known or known[entry.path][1] != entry.size
    ]
    # New files first, then changed ones least recently indexed first: a
    # source that keeps running out of time (live logs change size every
    # sweep) still works through all of its files over successive sweeps,
    # instead of re-reading the same first few each time (#218).
    pending.sort(
        key=lambda entry: ((1, known[entry.path][2]) if entry.path in known else (0, datetime.min))
    )

    attempted = 0
    for position, entry in enumerate(pending):
        # At least one file per call, so a single slow file still progresses.
        if deadline is not None and attempted and time.monotonic() >= deadline:
            stats.deferred = len(pending) - position
            logger.info(
                "search_index.source_budget_reached",
                source_id=source_id,
                deferred=stats.deferred,
            )
            break
        previous = known.get(entry.path)
        if not _still_indexed(session, source):
            logger.info("search_index.source_stopped", source_id=source_id)
            return stats

        attempted += 1
        try:
            content = _read_text(connector, source, entry, max_bytes)
        except _UNREADABLE as exc:
            # One file the service account can't read, or that rotated away
            # since the listing, mustn't fail the whole source (#204). The
            # previous index of that file, if any, is kept.
            logger.debug(
                "search_index.file_unreadable",
                source_id=source_id,
                path=entry.path,
                error=type(exc).__name__,
            )
            stats.skipped += 1
            stats.unreadable += 1
            _record_attempt(session, source_id, entry, previous)
            continue
        if content is None:
            stats.skipped += 1
            _record_attempt(session, source_id, entry, previous)
            continue

        _delete_fts_rows(session, source_id, entry.path)
        _insert_fts_rows(session, source_id, entry.path, content)
        _record_attempt(session, source_id, entry, previous)
        stats.indexed += 1

    # The listing above is complete even when time ran out, so files that
    # are gone (or no longer rule-visible) are removed either way.
    for path, (state_id, _, _) in known.items():
        if path not in seen_paths:
            _delete_fts_rows(session, source_id, path)
            _delete_alert_file_states(session, source_id, path)
            state = session.get(SearchIndexState, state_id)
            if state is not None:
                session.delete(state)
            stats.removed += 1

    session.commit()
    logger.info(
        "search_index.source_indexed",
        source_id=source_id,
        indexed=stats.indexed,
        skipped=stats.skipped,
        unreadable=stats.unreadable,
        removed=stats.removed,
        deferred=stats.deferred,
    )
    return stats


def _record_attempt(session: Session, source_id: int, entry: DirEntry, previous) -> None:
    """Records that `entry` was handled at its current size, whether it was
    indexed, skipped (binary, a .gz that won't decompress) or unreadable.
    A skipped or unreadable file is then retried only when its size
    changes, instead of counting as new on every sweep, going first in the
    queue and using up the source's time budget (#218). Its earlier index
    rows, if any, are kept. Commits."""
    state = session.get(SearchIndexState, previous[0]) if previous else None
    if state is None:
        state = SearchIndexState(source_id=source_id, file_path=entry.path)
    state.size = entry.size
    state.indexed_at = utcnow()
    session.add(state)
    session.commit()


def _still_indexed(session: Session, source: Source) -> bool:
    """Re-reads the source: with a commit per file, an admin can turn
    indexing off, disable or delete the source while a sweep runs."""
    try:
        session.refresh(source)
    except InvalidRequestError:  # deleted meanwhile
        return False
    return bool(source.enabled and source.search_indexing_enabled)


def purge_inactive_source_indexes(session: Session) -> int:
    """Backstop for the immediate purge in api/sources.py: drops indexed
    content for any source that's disabled or no longer opted in, however it
    got that way (e.g. an older version that never purged). Commits."""
    indexed_ids = set(session.exec(select(SearchIndexState.source_id).distinct()).all())
    if not indexed_ids:
        return 0
    active_ids = set(
        session.exec(
            select(Source.id).where(
                Source.id.in_(indexed_ids),
                Source.search_indexing_enabled.is_(True),
                Source.enabled.is_(True),
            )
        ).all()
    )
    stale = indexed_ids - active_ids
    for source_id in stale:
        delete_source_index(session, source_id)
    session.commit()
    if stale:
        logger.info("search_index.purged_inactive_sources", source_ids=sorted(stale))
    return len(stale)


def run_indexing_sweep() -> None:
    """The APScheduler job (see app/main.py's lifespan) — indexes every
    enabled, opted-in source in turn, each within its time budget
    (search_index_source_budget_seconds). One source's failure (a dead
    connection, a permissions error) is logged and skipped rather than
    aborting the sweep for every other source."""
    from app.db import engine  # deferred: app.db imports this module for schema setup

    budget = get_settings().search_index_source_budget_seconds
    with Session(engine) as session:
        purge_inactive_source_indexes(session)
        sources = session.exec(
            select(Source).where(Source.search_indexing_enabled.is_(True), Source.enabled.is_(True))
        ).all()
        for source in sources:
            deadline = time.monotonic() + budget if budget > 0 else None
            try:
                index_source(session, source, deadline)
            except Exception:
                logger.exception("search_index.sweep_failed", source_id=source.id)

    record_search_sweep()

    # Alerting (app.alerts) rides on this same sweep rather than its own
    # scheduler job -- it needs to see this sweep's freshly-updated
    # SearchIndexState.indexed_at timestamps, so it runs right after,
    # never concurrently with or ahead of indexing.
    from app.alerts import evaluate_alerts

    evaluate_alerts()


_HIGHLIGHT_START = "\x01"
_HIGHLIGHT_END = "\x02"


def _escape_snippet(raw: str) -> str:
    """FTS5's snippet() splices its start/end markers into the *raw, stored*
    line — unlike Jinja/HTML templating, it does no escaping of its own.
    A log line is arbitrary attacker-influenced text (anything that ended up
    in a log), so passing it straight to the frontend's `{@html}` with
    literal `<mark>`/`</mark>` markers already baked in would be a stored-XSS
    hole: a log line containing `<script>...</script>` would render as-is.
    Instead the query asks FTS5 to bracket matches with control characters
    that can't appear in real text, HTML-escapes the *entire* result (which
    leaves those control characters untouched, since they aren't HTML
    metacharacters), and only then swaps them for the real <mark> tags —
    so any actual angle brackets/ampersands in the log content are safely
    escaped first."""
    escaped = html.escape(raw)
    return escaped.replace(_HIGHLIGHT_START, "<mark>").replace(_HIGHLIGHT_END, "</mark>")


def _path_filter_sql(path_filter: str) -> tuple[str, str]:
    """A wildcard filter (`*`, `?`) is a GLOB over the whole path; anything
    else matches as a case-insensitive substring (#163)."""
    if "*" in path_filter or "?" in path_filter:
        return " AND file_path GLOB :path_filter", path_filter
    escaped = path_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return " AND file_path LIKE :path_filter ESCAPE '\\'", f"%{escaped}%"


def search(
    session: Session,
    query: str,
    source_ids: set[int] | None,
    limit: int = 50,
    *,
    path_filter: str = "",
) -> list[SearchHit]:
    """Runs `query` against the FTS5 index, scoped to `source_ids` (None
    means "all", per auth.rbac.visible_source_ids' convention — reserved for
    super-admins; an empty set means "none" and short-circuits without a
    query at all). The user's raw input is wrapped as a single quoted FTS5
    phrase rather than passed through as its own query syntax — predictable
    substring-ish matching beats exposing FTS5's full AND/OR/NOT/prefix* DSL
    to a plain search box, and avoids a MATCH syntax error on input like
    unbalanced quotes or a bare operator.

    `file_path` is an indexed column alongside `snippet`, so one unified
    MATCH query covers both content and path/filename matches, ranked
    together (see ROADMAP.md's path/host matching notes). A file whose path
    matches but whose lines don't is real signal — "find this file even if
    none of its lines say so" — but since the index is one row per line,
    a path-only match is technically true of *every* line in that file, and
    returning all of them would flood the results with near-duplicates for
    one file. So each row's own snippet() call against the content column
    is used to tell content matches (a real highlight) from path-only ones
    (no highlight there), and path-only matches are deduplicated down to one
    representative row per (source_id, file_path)."""
    if source_ids is not None and not source_ids:
        return []

    match_query = '"' + query.replace('"', '""') + '"'
    sql = (
        "SELECT source_id, file_path, line_number, "
        "snippet(search_index_fts, 3, :highlight_start, :highlight_end, '…', 12) "
        "AS content_highlighted, "
        "snippet(search_index_fts, 1, :highlight_start, :highlight_end, '…', 64) "
        "AS path_highlighted "
        "FROM search_index_fts WHERE search_index_fts MATCH :query"
    )
    # Over-fetch before dedup: a run of many lines from the same
    # path-matched file would otherwise collapse to far fewer than `limit`
    # final results.
    params: dict[str, object] = {
        "query": match_query,
        "raw_limit": max(limit * 5, limit),
        "highlight_start": _HIGHLIGHT_START,
        "highlight_end": _HIGHLIGHT_END,
    }
    if source_ids is not None:
        placeholders = ", ".join(f":sid{i}" for i in range(len(source_ids)))
        sql += f" AND source_id IN ({placeholders})"
        params.update({f"sid{i}": sid for i, sid in enumerate(source_ids)})
    if path_filter:
        clause, params["path_filter"] = _path_filter_sql(path_filter)
        sql += clause
    sql += " ORDER BY rank LIMIT :raw_limit"

    rows = session.execute(text(sql), params).all()

    hits: list[SearchHit] = []
    seen_path_matches: set[tuple[int, str]] = set()
    for row in rows:
        if len(hits) >= limit:
            break
        is_content_match = _HIGHLIGHT_START in row.content_highlighted
        if is_content_match:
            hits.append(
                SearchHit(
                    source_id=row.source_id,
                    file_path=row.file_path,
                    line_number=row.line_number,
                    snippet_html=_escape_snippet(row.content_highlighted),
                    matched_field="content",
                )
            )
            continue

        path_key = (row.source_id, row.file_path)
        if path_key in seen_path_matches:
            continue
        seen_path_matches.add(path_key)
        hits.append(
            SearchHit(
                source_id=row.source_id,
                file_path=row.file_path,
                line_number=row.line_number,
                snippet_html=_escape_snippet(row.path_highlighted),
                matched_field="path",
            )
        )

    return hits


def last_indexed_line(session: Session, source_id: int, file_path: str) -> int:
    """Highest line number currently indexed for a file (0 if none)."""
    row = session.execute(
        text(
            "SELECT MAX(line_number) FROM search_index_fts "
            "WHERE source_id = :source_id AND file_path = :file_path"
        ),
        {"source_id": source_id, "file_path": file_path},
    ).first()
    return int(row[0] or 0) if row else 0


def search_file_content_after(
    session: Session, query: str, source_id: int, file_path: str, after_line: int, limit: int
) -> list[SearchHit]:
    """Content matches in one file past `after_line`, in line order -- what
    app.alerts needs to find new lines, with no global top-N cut that could
    push a new match out behind better-ranked old ones."""
    match_query = 'snippet:"' + query.replace('"', '""') + '"'
    rows = session.execute(
        text(
            "SELECT source_id, file_path, line_number, "
            "snippet(search_index_fts, 3, :highlight_start, :highlight_end, '…', 12) "
            "AS content_highlighted "
            "FROM search_index_fts WHERE search_index_fts MATCH :query "
            "AND source_id = :source_id AND file_path = :file_path "
            "AND line_number > :after_line ORDER BY line_number LIMIT :limit"
        ),
        {
            "query": match_query,
            "source_id": source_id,
            "file_path": file_path,
            "after_line": after_line,
            "limit": limit,
            "highlight_start": _HIGHLIGHT_START,
            "highlight_end": _HIGHLIGHT_END,
        },
    ).all()
    return [
        SearchHit(
            source_id=row.source_id,
            file_path=row.file_path,
            line_number=row.line_number,
            snippet_html=_escape_snippet(row.content_highlighted),
            matched_field="content",
        )
        for row in rows
    ]
