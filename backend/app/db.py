from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  registers tables on SQLModel.metadata
from app.auth import models as auth_models  # noqa: F401  registers tables on SQLModel.metadata
from app.config import get_settings

# How long a write waits for another writer to finish before failing with
# "database is locked" (#188). Writers hold the lock only briefly (the
# search indexer commits file by file), so this is a backstop, not the fix.
SQLITE_BUSY_TIMEOUT_SECONDS = 30

settings = get_settings()
connect_args = {}
_sqlite_file = False
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
    db_path = settings.database_url.removeprefix("sqlite:///")
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _sqlite_file = True
engine = create_engine(settings.database_url, connect_args=connect_args)


def configure_sqlite_connection(dbapi_connection, _record=None) -> None:
    """WAL lets requests read while a write is in progress, and a writer no
    longer blocks readers at commit (#188). The setting is stored in the
    database file, so this only changes something on the first connection
    to a database that was still in rollback-journal mode."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
    finally:
        cursor.close()


if _sqlite_file:
    event.listen(engine, "connect", configure_sqlite_connection)


def init_db() -> None:
    from app.name_index_store import ensure_name_index_schema

    SQLModel.metadata.create_all(engine)
    ensure_name_index_schema(engine)


def get_session():
    with Session(engine) as session:
        yield session
