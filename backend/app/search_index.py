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
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, select

from app.archives import decompress_gzip, is_archive, is_transparent_gzip
from app.collectors import agent as agent_collector
from app.collectors import local as local_collector
from app.collectors import smb as smb_collector
from app.collectors import ssh as ssh_collector
from app.collectors import winrm as winrm_collector
from app.collectors.base import DirEntry
from app.config import get_settings
from app.logging_config import get_logger
from app.models import Protocol, Rule, SearchIndexState, Source
from app.timeutils import utcnow

logger = get_logger(__name__)

_CONNECTORS = {
    Protocol.ssh: ssh_collector,
    Protocol.smb: smb_collector,
    Protocol.winrm: winrm_collector,
    Protocol.local: local_collector,
    Protocol.agent: agent_collector,
}

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


@dataclass
class SearchHit:
    source_id: int
    file_path: str
    line_number: int
    snippet_html: str = field(repr=False)


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
                    decompress_gzip(local_path, decompressed)
                except OSError:
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


def index_source(session: Session, source: Source) -> IndexStats:
    """Indexes (or re-indexes) every rule-visible file in `source`. Safe to
    call repeatedly — unchanged files (by size, see SearchIndexState's
    docstring on why size alone) are skipped, and files no longer present or
    no longer rule-visible are removed from the index."""
    connector = _CONNECTORS.get(source.protocol)
    if connector is None:
        return IndexStats()

    settings = get_settings()
    max_bytes = int(settings.search_index_max_file_size_mb * 1024 * 1024)
    rules = list(session.exec(select(Rule).where(Rule.source_id == source.id)).all())

    stats = IndexStats()
    seen_paths: set[str] = set()
    existing_by_path = {
        state.file_path: state
        for state in session.exec(
            select(SearchIndexState).where(SearchIndexState.source_id == source.id)
        ).all()
    }

    for entry in _iter_files(connector, source, rules):
        seen_paths.add(entry.path)
        existing = existing_by_path.get(entry.path)
        if existing is not None and existing.size == entry.size:
            continue

        content = _read_text(connector, source, entry, max_bytes)
        if content is None:
            stats.skipped += 1
            continue

        _delete_fts_rows(session, source.id, entry.path)
        _insert_fts_rows(session, source.id, entry.path, content)
        if existing is not None:
            existing.size = entry.size
            existing.indexed_at = utcnow()
            session.add(existing)
        else:
            session.add(
                SearchIndexState(
                    source_id=source.id,
                    file_path=entry.path,
                    size=entry.size,
                    indexed_at=utcnow(),
                )
            )
        stats.indexed += 1

    for path, state in existing_by_path.items():
        if path not in seen_paths:
            _delete_fts_rows(session, source.id, path)
            session.delete(state)
            stats.removed += 1

    session.commit()
    logger.info(
        "search_index.source_indexed",
        source_id=source.id,
        indexed=stats.indexed,
        skipped=stats.skipped,
        removed=stats.removed,
    )
    return stats


def run_indexing_sweep() -> None:
    """The APScheduler job (see app/main.py's lifespan) — indexes every
    enabled, opted-in source in turn. One source's failure (a dead
    connection, a permissions error) is logged and skipped rather than
    aborting the sweep for every other source."""
    from app.db import engine  # deferred: app.db imports this module for schema setup

    with Session(engine) as session:
        sources = session.exec(
            select(Source).where(Source.search_indexing_enabled.is_(True), Source.enabled.is_(True))
        ).all()
        for source in sources:
            try:
                index_source(session, source)
            except Exception:
                logger.exception("search_index.sweep_failed", source_id=source.id)


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


def search(
    session: Session, query: str, source_ids: set[int] | None, limit: int = 50
) -> list[SearchHit]:
    """Runs `query` against the FTS5 index, scoped to `source_ids` (None
    means "all", per auth.rbac.visible_source_ids' convention — reserved for
    super-admins; an empty set means "none" and short-circuits without a
    query at all). The user's raw input is wrapped as a single quoted FTS5
    phrase rather than passed through as its own query syntax — predictable
    substring-ish matching beats exposing FTS5's full AND/OR/NOT/prefix* DSL
    to a plain search box, and avoids a MATCH syntax error on input like
    unbalanced quotes or a bare operator."""
    if source_ids is not None and not source_ids:
        return []

    match_query = '"' + query.replace('"', '""') + '"'
    sql = (
        "SELECT source_id, file_path, line_number, "
        "snippet(search_index_fts, 3, :highlight_start, :highlight_end, '…', 12) AS highlighted "
        "FROM search_index_fts WHERE search_index_fts MATCH :query"
    )
    params: dict[str, object] = {
        "query": match_query,
        "limit": limit,
        "highlight_start": _HIGHLIGHT_START,
        "highlight_end": _HIGHLIGHT_END,
    }
    if source_ids is not None:
        placeholders = ", ".join(f":sid{i}" for i in range(len(source_ids)))
        sql += f" AND source_id IN ({placeholders})"
        params.update({f"sid{i}": sid for i, sid in enumerate(source_ids)})
    sql += " ORDER BY rank LIMIT :limit"

    rows = session.execute(text(sql), params).all()
    return [
        SearchHit(
            source_id=row.source_id,
            file_path=row.file_path,
            line_number=row.line_number,
            snippet_html=_escape_snippet(row.highlighted),
        )
        for row in rows
    ]
