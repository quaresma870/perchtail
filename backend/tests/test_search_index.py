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
