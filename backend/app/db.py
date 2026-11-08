from datetime import timedelta
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  registers tables on SQLModel.metadata
from app.auth import models as auth_models  # noqa: F401  registers tables on SQLModel.metadata
from app.config import get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)

# How long a write waits for another writer to finish before failing with
# "database is locked" (#188). Writers hold the lock only briefly (the
# search indexer commits file by file), so this is a backstop, not the fix.
SQLITE_BUSY_TIMEOUT_SECONDS = 30
# What the WAL file is cut back to after a checkpoint (#346): without a
# limit it keeps the size of its biggest burst of writes.
SQLITE_JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

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
        cursor.execute(f"PRAGMA journal_size_limit={SQLITE_JOURNAL_SIZE_LIMIT_BYTES}")
    finally:
        cursor.close()


if _sqlite_file:
    event.listen(engine, "connect", configure_sqlite_connection)


def sqlite_file() -> Path | None:
    """The SQLite database file, or None for another database or an
    in-memory one."""
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    path = url.removeprefix("sqlite:///")
    return Path(path) if path and path != ":memory:" else None


def run_wal_checkpoint() -> None:
    """The hourly job (#346): copies the WAL back into the database and
    truncates it, so the WAL file doesn't keep growing between SQLite's own
    automatic checkpoints, which never shrink it. A checkpoint that can't
    finish because readers are busy is tried again next hour."""
    from app.db import engine as current  # deferred, so tests can patch app.db.engine

    if current.dialect.name != "sqlite":
        return
    with current.connect() as connection:
        busy, wal_pages, moved = connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
    log = logger.warning if busy else logger.info
    log("db.wal_checkpoint", busy=bool(busy), wal_pages=wal_pages, checkpointed=moved)


WAL_CHECKPOINT_INTERVAL = timedelta(hours=1)


def register_upkeep_jobs(scheduler) -> None:
    """Database upkeep (#346), registered at startup (app/main.py)."""
    from app.jobs import register

    register(
        scheduler,
        "wal_checkpoint",
        "Database WAL checkpoint",
        run_wal_checkpoint,
        WAL_CHECKPOINT_INTERVAL,
    )


def init_db() -> None:
    from app.name_index_store import ensure_name_index_schema

    SQLModel.metadata.create_all(engine)
    ensure_name_index_schema(engine)


def get_session():
    with Session(engine) as session:
        yield session
