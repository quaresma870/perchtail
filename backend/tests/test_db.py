from app.db import ensure_search_schema
from app.models import SearchIndexState
from app.timeutils import utcnow
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


def _fresh_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _fts_table_sql(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'search_index_fts'"
            )
        ).scalar()


def test_ensure_search_schema_creates_file_path_as_indexed(tmp_path):
    engine = _fresh_engine()
    ensure_search_schema(engine)

    assert "file_path UNINDEXED" not in _fts_table_sql(engine)


def test_ensure_search_schema_is_idempotent(tmp_path):
    engine = _fresh_engine()
    ensure_search_schema(engine)
    first_sql = _fts_table_sql(engine)

    ensure_search_schema(engine)

    assert _fts_table_sql(engine) == first_sql


def test_ensure_search_schema_upgrades_a_table_with_the_old_unindexed_path_column(tmp_path):
    engine = _fresh_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE search_index_fts "
                "USING fts5(source_id UNINDEXED, file_path UNINDEXED, "
                "line_number UNINDEXED, snippet)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO search_index_fts (source_id, file_path, line_number, snippet) "
                "VALUES (1, 'old.log', 1, 'stale row from before the upgrade')"
            )
        )
    with Session(engine) as session:
        session.add(
            SearchIndexState(source_id=1, file_path="old.log", size=10, indexed_at=utcnow())
        )
        session.commit()

    ensure_search_schema(engine)

    assert "file_path UNINDEXED" not in _fts_table_sql(engine)
    with engine.connect() as conn:
        row_count = conn.execute(text("SELECT COUNT(*) FROM search_index_fts")).scalar()
    assert row_count == 0
    with Session(engine) as session:
        assert session.exec(select(SearchIndexState)).first() is None
