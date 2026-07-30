from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  registers tables on SQLModel.metadata
from app.auth import models as auth_models  # noqa: F401  registers tables on SQLModel.metadata
from app.config import get_settings

settings = get_settings()
connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
    db_path = settings.database_url.removeprefix("sqlite:///")
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(settings.database_url, connect_args=connect_args)


def ensure_search_schema(target_engine=engine) -> None:
    """Creates the Phase 3 full-text search FTS5 virtual table (see
    app/search_index.py) if it doesn't already exist. Not a SQLModel/
    SQLAlchemy table class — FTS5 tables aren't representable as one — so it
    can't be created by SQLModel.metadata.create_all() and needs this
    explicit, idempotent raw-SQL step alongside it. Called from init_db()
    (app startup) and directly by the migration and by tests' `session`
    fixture, so every path that provisions a fresh schema covers this too."""
    with target_engine.begin() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS search_index_fts "
                "USING fts5(source_id UNINDEXED, file_path UNINDEXED, "
                "line_number UNINDEXED, snippet)"
            )
        )


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    ensure_search_schema(engine)


def get_session():
    with Session(engine) as session:
        yield session
