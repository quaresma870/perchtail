import gzip
import zipfile

from app.db import ensure_search_schema
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from app.search_index import index_source, run_indexing_sweep, search
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


def _source(session, tmp_path, **overrides) -> Source:
    defaults = dict(
        name="app01",
        protocol=Protocol.local,
        host="localhost",
        base_path=str(tmp_path),
        search_indexing_enabled=True,
    )
    defaults.update(overrides)
    source = Source(**defaults)
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _rule(
    session, source_id, pattern, order=0, kind=PatternKind.glob, type_=RuleType.include
) -> Rule:
    rule = Rule(source_id=source_id, order=order, type=type_, pattern=pattern, pattern_kind=kind)
    session.add(rule)
    session.commit()
    return rule


def _fts_rows(session, source_id):
    return session.execute(
        text(
            "SELECT file_path, line_number, snippet FROM search_index_fts "
            "WHERE source_id = :source_id ORDER BY line_number"
        ),
        {"source_id": source_id},
    ).all()


def test_index_source_indexes_visible_lines(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("first line\nsecond line\n")

    stats = index_source(session, source)

    assert stats.indexed == 1
    rows = _fts_rows(session, source.id)
    assert [r.snippet for r in rows] == ["first line", "second line"]
    assert [r.line_number for r in rows] == [1, 2]


def test_index_source_skips_rule_excluded_files(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "secret.txt").write_text("do not index me\n")

    stats = index_source(session, source)

    assert stats.indexed == 0
    assert _fts_rows(session, source.id) == []


def test_index_source_skips_binary_files(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_bytes(b"\x00\x01binary junk\xff\xfe")

    stats = index_source(session, source)

    assert stats.indexed == 0
    assert stats.skipped == 1
    assert _fts_rows(session, source.id) == []


def test_index_source_is_idempotent_for_unchanged_file(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("same content\n")

    index_source(session, source)
    second = index_source(session, source)

    assert second.indexed == 0
    assert len(_fts_rows(session, source.id)) == 1


def test_index_source_reindexes_when_size_changes(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    log_path = tmp_path / "app.log"
    log_path.write_text("v1\n")
    index_source(session, source)

    log_path.write_text("v1 grown considerably\n")
    stats = index_source(session, source)

    assert stats.indexed == 1
    rows = _fts_rows(session, source.id)
    assert [r.snippet for r in rows] == ["v1 grown considerably"]


def test_index_source_removes_entries_for_deleted_files(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    log_path = tmp_path / "app.log"
    log_path.write_text("about to vanish\n")
    index_source(session, source)
    assert len(_fts_rows(session, source.id)) == 1

    log_path.unlink()
    stats = index_source(session, source)

    assert stats.removed == 1
    assert _fts_rows(session, source.id) == []


def test_index_source_transparently_indexes_gzip(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log.gz")
    with gzip.open(tmp_path / "app.log.gz", "wb") as f:
        f.write(b"gzipped line one\ngzipped line two\n")

    stats = index_source(session, source)

    assert stats.indexed == 1
    rows = _fts_rows(session, source.id)
    assert [r.snippet for r in rows] == ["gzipped line one", "gzipped line two"]


def test_index_source_skips_archive_containers(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.zip")
    with zipfile.ZipFile(tmp_path / "bundle.zip", "w") as zf:
        zf.writestr("inner.log", "should not be indexed\n")

    stats = index_source(session, source)

    assert stats.indexed == 0
    assert stats.skipped == 1
    assert _fts_rows(session, source.id) == []


def test_index_source_skips_files_over_max_size(session, tmp_path, monkeypatch):
    from app import search_index as search_index_module

    class TinySettings:
        search_index_max_file_size_mb = 0.000001  # ~1 byte

    monkeypatch.setattr(search_index_module, "get_settings", lambda: TinySettings())

    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("this is definitely more than one byte\n")

    stats = index_source(session, source)

    assert stats.indexed == 0
    assert stats.skipped == 1


def test_search_scopes_results_to_permitted_source_ids(session, tmp_path):
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    source_a = _source(session, dir_a, name="source-a")
    source_b = _source(session, dir_b, name="source-b")
    _rule(session, source_a.id, "**/*.log")
    _rule(session, source_b.id, "**/*.log")
    (dir_a / "app.log").write_text("shared keyword alpha\n")
    (dir_b / "app.log").write_text("shared keyword beta\n")
    index_source(session, source_a)
    index_source(session, source_b)

    all_hits = search(session, "shared keyword", source_ids=None)
    assert {h.source_id for h in all_hits} == {source_a.id, source_b.id}

    scoped_hits = search(session, "shared keyword", source_ids={source_a.id})
    assert {h.source_id for h in scoped_hits} == {source_a.id}

    assert search(session, "shared keyword", source_ids=set()) == []


def test_search_highlights_matched_term(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    hits = search(session, "refused", source_ids=None)

    assert len(hits) == 1
    assert "<mark>refused</mark>" in hits[0].snippet_html


def test_search_escapes_html_in_log_content(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("<script>alert('refused')</script>\n")
    index_source(session, source)

    hits = search(session, "refused", source_ids=None)

    assert len(hits) == 1
    assert "<script>" not in hits[0].snippet_html
    assert "&lt;script&gt;" in hits[0].snippet_html
    assert "<mark>refused</mark>" in hits[0].snippet_html


def test_search_returns_no_hits_for_unmatched_query(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("all is well\n")
    index_source(session, source)

    assert search(session, "nonexistent-term-xyz", source_ids=None) == []


def test_search_marks_content_hits_as_matched_field_content(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    hits = search(session, "refused", source_ids=None)

    assert len(hits) == 1
    assert hits[0].matched_field == "content"


def test_search_matches_file_path_even_when_no_line_contains_the_query(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "win-app-02.log").write_text("first line\nsecond line\nthird line\n")
    index_source(session, source)

    hits = search(session, "win-app-02", source_ids=None)

    assert len(hits) == 1
    assert hits[0].matched_field == "path"
    assert hits[0].file_path == "win-app-02.log"
    assert "<mark>win-app-02</mark>" in hits[0].snippet_html


def test_search_dedupes_path_only_matches_to_one_hit_per_file(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    lines = "\n".join(f"line {i}" for i in range(20))
    (tmp_path / "access-win-app-02.log").write_text(lines + "\n")
    index_source(session, source)

    hits = search(session, "win-app-02", source_ids=None)

    # 20 lines all share the matching path, but should collapse to a
    # single representative hit rather than flooding results with
    # near-duplicates of the same file.
    assert len(hits) == 1
    assert hits[0].matched_field == "path"


def test_search_prefers_content_matches_over_path_matches_for_the_same_row(session, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "keyword.log").write_text("this line mentions keyword directly\n")
    index_source(session, source)

    hits = search(session, "keyword", source_ids=None)

    assert len(hits) == 1
    assert hits[0].matched_field == "content"


def test_run_indexing_sweep_only_processes_enabled_opted_in_sources(tmp_path, monkeypatch):
    # run_indexing_sweep() opens its own session against app.db.engine
    # (deferred import, since app.db itself depends on this module for
    # schema setup) rather than accepting one as a parameter, so this test
    # points that at a throwaway engine instead of reusing the `session`
    # fixture's — same StaticPool/in-memory setup conftest.py uses.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    ensure_search_schema(engine)
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        opted_in_dir = tmp_path / "opted-in"
        opted_in_dir.mkdir()
        opted_in = _source(session, opted_in_dir, name="opted-in")
        _rule(session, opted_in.id, "**/*.log")
        (opted_in_dir / "app.log").write_text("indexed content here\n")

        not_opted_in_dir = tmp_path / "not-opted-in"
        not_opted_in_dir.mkdir()
        not_opted_in = _source(
            session, not_opted_in_dir, name="not-opted-in", search_indexing_enabled=False
        )
        _rule(session, not_opted_in.id, "**/*.log")
        (not_opted_in_dir / "app.log").write_text("should stay unindexed\n")

        opted_in_id, not_opted_in_id = opted_in.id, not_opted_in.id

    run_indexing_sweep()

    with Session(engine) as session:
        assert len(_fts_rows(session, opted_in_id)) == 1
        assert _fts_rows(session, not_opted_in_id) == []


def test_sweep_purges_snippets_left_behind_by_an_opted_out_source(tmp_path, monkeypatch):
    from app.models import SearchIndexState
    from app.search_index import _insert_fts_rows
    from app.timeutils import utcnow
    from sqlmodel import select

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    ensure_search_schema(engine)
    monkeypatch.setattr("app.db.engine", engine)
    monkeypatch.setattr("app.alerts.evaluate_alerts", lambda: None)

    with Session(engine) as session:
        source = _source(session, tmp_path, search_indexing_enabled=False)
        _insert_fts_rows(session, source.id, "app.log", "secret token here")
        session.add(
            SearchIndexState(source_id=source.id, file_path="app.log", size=1, indexed_at=utcnow())
        )
        session.commit()
        source_id = source.id

    run_indexing_sweep()

    with Session(engine) as session:
        assert _fts_rows(session, source_id) == []
        assert session.exec(select(SearchIndexState)).all() == []


class _HookedLocalConnector:
    """The local connector, with a callback run while each file is fetched
    -- the point where a remote fetch spends its time."""

    def __init__(self, on_fetch):
        from app.collectors import local

        self._local = local
        self._on_fetch = on_fetch

    def list_directory(self, source, rules, relative_path=""):
        return self._local.list_directory(source, rules, relative_path)

    def local_copy(self, source, relative_path):
        self._on_fetch(relative_path)
        return self._local.local_copy(source, relative_path)


def _file_engine(tmp_path):
    from app.db import configure_sqlite_connection
    from sqlalchemy import event

    engine = create_engine(
        f"sqlite:///{tmp_path / 'perchtail.db'}", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", configure_sqlite_connection)
    SQLModel.metadata.create_all(engine)
    ensure_search_schema(engine)
    return engine


def test_indexing_does_not_hold_the_write_lock_while_fetching(tmp_path, monkeypatch):
    """#188: the first sweep of a large source kept one write transaction
    open for minutes and every login failed with "database is locked".
    Another writer that won't wait at all must get through while a file is
    being fetched, after earlier files were already written."""
    import sqlite3

    import app.search_index as search_index_module

    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("a.log", "b.log", "c.log"):
        (logs / name).write_text(f"line in {name}\n")
    engine = _file_engine(tmp_path)
    writes = []

    def write_elsewhere(relative_path):
        other = sqlite3.connect(tmp_path / "perchtail.db", timeout=0)
        try:
            other.execute("UPDATE source SET name = ?", (relative_path,))
            other.commit()
            writes.append(relative_path)
        finally:
            other.close()

    monkeypatch.setattr(
        search_index_module, "_CONNECTORS", {Protocol.local: _HookedLocalConnector(write_elsewhere)}
    )
    with Session(engine) as session:
        source = _source(session, logs)
        _rule(session, source.id, "*.log")
        stats = index_source(session, source)

    assert stats.indexed == 3
    assert writes == ["a.log", "b.log", "c.log"]
    with sqlite3.connect(tmp_path / "perchtail.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_indexing_stops_when_turned_off_mid_sweep(tmp_path, monkeypatch):
    import sqlite3

    import app.search_index as search_index_module

    for name in ("a.log", "b.log", "c.log"):
        (tmp_path / name).write_text(f"line in {name}\n")
    engine = _file_engine(tmp_path)
    db = tmp_path / "perchtail.db"

    def turn_off(relative_path):
        if relative_path == "a.log":
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE source SET search_indexing_enabled = 0")

    monkeypatch.setattr(
        search_index_module, "_CONNECTORS", {Protocol.local: _HookedLocalConnector(turn_off)}
    )
    with Session(engine) as session:
        source = _source(session, tmp_path)
        _rule(session, source.id, "*.log")
        stats = index_source(session, source)

    assert stats.indexed == 1


def test_indexing_stops_when_the_source_is_deleted_mid_sweep(tmp_path, monkeypatch):
    import sqlite3

    import app.search_index as search_index_module

    for name in ("a.log", "b.log"):
        (tmp_path / name).write_text(f"line in {name}\n")
    engine = _file_engine(tmp_path)
    db = tmp_path / "perchtail.db"

    def delete_source(relative_path):
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM rule")
            conn.execute("DELETE FROM source")

    monkeypatch.setattr(
        search_index_module,
        "_CONNECTORS",
        {Protocol.local: _HookedLocalConnector(delete_source)},
    )
    with Session(engine) as session:
        source = _source(session, tmp_path)
        _rule(session, source.id, "*.log")
        stats = index_source(session, source)

    assert stats.indexed == 1


def test_scheduler_runs_late_jobs_instead_of_skipping_them():
    """F-I: the default one-second grace skipped indexing sweeps (and the
    alert evaluation riding on them) whenever the worker was busy."""
    from app.scheduler import scheduler

    defaults = scheduler._job_defaults
    assert defaults["misfire_grace_time"] >= 60
    assert defaults["coalesce"] is True


def test_an_unreadable_file_is_skipped_not_fatal_to_the_source(tmp_path, monkeypatch):
    """#204: one file the service account can't read failed the whole
    source's sweep, every sweep."""
    import app.search_index as search_index_module
    from app.collectors.base import SourcePermissionError

    for name in ("a.log", "locked.log", "c.log"):
        (tmp_path / name).write_text(f"line in {name}\n")
    engine = _file_engine(tmp_path / "")

    def refuse(relative_path):
        if relative_path == "locked.log":
            raise SourcePermissionError("Permission denied")

    monkeypatch.setattr(
        search_index_module, "_CONNECTORS", {Protocol.local: _HookedLocalConnector(refuse)}
    )
    with Session(engine) as session:
        source = _source(session, tmp_path)
        _rule(session, source.id, "*.log")
        stats = index_source(session, source)
        files = {r.file_path for r in _fts_rows(session, source.id)}

    assert stats.indexed == 2
    assert stats.unreadable == 1 and stats.skipped == 1
    assert files == {"a.log", "c.log"}


# --- #218: a per-source time budget, so one large source can't hold the sweep


def _fetch_recorder(monkeypatch):
    import app.search_index as search_index_module

    fetched: list[str] = []
    monkeypatch.setattr(
        search_index_module, "_CONNECTORS", {Protocol.local: _HookedLocalConnector(fetched.append)}
    )
    return fetched


def test_a_source_past_its_deadline_reads_one_file_and_defers_the_rest(
    session, tmp_path, monkeypatch
):
    fetched = _fetch_recorder(monkeypatch)
    for name in ("a.log", "b.log", "c.log"):
        (tmp_path / name).write_text(f"line in {name}\n")
    source = _source(session, tmp_path)
    _rule(session, source.id, "*.log")

    first = index_source(session, source, deadline=0.0)  # already past
    assert (first.indexed, first.deferred) == (1, 2)

    second = index_source(session, source)
    assert (second.indexed, second.deferred) == (2, 0)
    # The file indexed in the first call isn't read again.
    assert len(fetched) == 3 and len(set(fetched)) == 3


def test_new_files_come_first_then_the_least_recently_indexed(session, tmp_path, monkeypatch):
    """Live logs change size every sweep: without this order, a source that
    keeps running out of time would re-read the same first files forever."""
    from datetime import timedelta

    from app.models import SearchIndexState
    from app.timeutils import utcnow
    from sqlmodel import select

    fetched = _fetch_recorder(monkeypatch)
    for name in ("a.log", "b.log", "c.log"):
        (tmp_path / name).write_text(f"line in {name}\n")
    source = _source(session, tmp_path)
    _rule(session, source.id, "*.log")
    index_source(session, source)

    ages = {"a.log": 1, "b.log": 3, "c.log": 2}  # minutes since indexed
    for state in session.exec(select(SearchIndexState)).all():
        state.indexed_at = utcnow() - timedelta(minutes=ages[state.file_path])
        session.add(state)
    session.commit()
    for name in ("a.log", "b.log", "c.log"):
        with open(tmp_path / name, "a") as handle:
            handle.write("another line\n")
    (tmp_path / "new.log").write_text("brand new\n")
    fetched.clear()

    order = []
    for _ in range(4):
        index_source(session, source, deadline=0.0)
        order.append(fetched[-1])

    assert order == ["new.log", "b.log", "c.log", "a.log"]


def test_files_that_are_gone_are_removed_even_when_time_runs_out(session, tmp_path, monkeypatch):
    _fetch_recorder(monkeypatch)
    (tmp_path / "old.log").write_text("going away\n")
    source = _source(session, tmp_path)
    _rule(session, source.id, "*.log")
    index_source(session, source)

    (tmp_path / "old.log").unlink()
    for name in ("a.log", "b.log"):
        (tmp_path / name).write_text(f"line in {name}\n")
    stats = index_source(session, source, deadline=0.0)

    assert (stats.indexed, stats.deferred, stats.removed) == (1, 1, 1)
    assert "old.log" not in {row.file_path for row in _fts_rows(session, source.id)}


def test_one_large_source_does_not_hold_the_sweep_for_the_others(tmp_path, monkeypatch):
    import app.search_index as search_index_module

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    ensure_search_schema(engine)
    monkeypatch.setattr("app.db.engine", engine)

    class TinyBudget:
        search_index_max_file_size_mb = 20.0
        search_index_source_budget_seconds = 1

    monkeypatch.setattr(search_index_module, "get_settings", lambda: TinyBudget())

    class SlowClock:  # every reading is 5 s after the last
        now = 0

        @classmethod
        def monotonic(cls):
            cls.now += 5
            return cls.now

    monkeypatch.setattr(search_index_module, "time", SlowClock)

    with Session(engine) as session:
        big_dir, small_dir = tmp_path / "big", tmp_path / "small"
        big_dir.mkdir()
        small_dir.mkdir()
        for index in range(5):
            (big_dir / f"{index}.log").write_text(f"big {index}\n")
        (small_dir / "app.log").write_text("small\n")
        big = _source(session, big_dir, name="big")
        small = _source(session, small_dir, name="small")
        _rule(session, big.id, "*.log")
        _rule(session, small.id, "*.log")
        big_id, small_id = big.id, small.id

    run_indexing_sweep()

    with Session(engine) as session:
        assert len(_fts_rows(session, big_id)) == 1
        assert len(_fts_rows(session, small_id)) == 1


def test_a_skipped_file_is_retried_only_when_its_size_changes(session, tmp_path, monkeypatch):
    """Review of #218: binary and unreadable files were never recorded, so
    every sweep treated them as new, put them first and fetched them again."""
    fetched = _fetch_recorder(monkeypatch)
    (tmp_path / "blob.log").write_bytes(b"\x00\x01binary")
    (tmp_path / "app.log").write_text("text\n")
    source = _source(session, tmp_path)
    _rule(session, source.id, "*.log")

    index_source(session, source)
    assert sorted(fetched) == ["app.log", "blob.log"]

    fetched.clear()
    index_source(session, source)
    assert fetched == []  # nothing changed, nothing fetched

    with open(tmp_path / "blob.log", "ab") as handle:
        handle.write(b"\x00more")
    index_source(session, source)
    assert fetched == ["blob.log"]
