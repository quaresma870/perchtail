import ipaddress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  registers tables on SQLModel.metadata
from app.auth import models as auth_models  # noqa: F401  registers tables on SQLModel.metadata
from app.config import Settings, get_settings, is_postgresql_url
from app.logging_config import get_logger

logger = get_logger(__name__)

# How long a write waits for another writer to finish before failing with
# "database is locked" (#188). Writers hold the lock only briefly (the
# file-name index commits in batches), so this is a backstop, not the fix.
SQLITE_BUSY_TIMEOUT_SECONDS = 30
# What the WAL file is cut back to after a checkpoint (#346): without a
# limit it keeps the size of its biggest burst of writes.
SQLITE_JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

# PostgreSQL (#306): the oldest server accepted (server_version_num), how
# long connecting may take, and how long a pooled connection is kept before
# it is replaced (a firewall or proxy may drop idle ones silently).
POSTGRESQL_MIN_SERVER_VERSION = 180000
POSTGRESQL_CONNECT_TIMEOUT_SECONDS = 10
POSTGRESQL_POOL_RECYCLE_SECONDS = 1800
# libpq's TCP keepalive options: probe after 30 s idle, every 10 s, 3 misses.
POSTGRESQL_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}
# libpq's sslmode values that check the server's certificate.
VERIFIED_SSLMODES = frozenset({"verify-ca", "verify-full"})


def engine_options(url: str, settings: Settings | None = None) -> dict[str, Any]:
    """create_engine's keyword arguments for this URL, shared by the app's
    engine and the migrations (migrations/env.py), which only replace the
    pool."""
    settings = settings or get_settings()
    if url.startswith("sqlite"):
        return {
            "connect_args": {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
        }
    if is_postgresql_url(url):
        return {
            # application_name shows PerchTail in pg_stat_activity; the URL's
            # own options (sslmode, sslrootcert, ...) are kept.
            # TCP keepalives notice a dead server or a cut connection, above
            # all on the idle one holding the instance lock (app/instance_lock.py).
            "connect_args": {
                "application_name": "perchtail",
                "connect_timeout": POSTGRESQL_CONNECT_TIMEOUT_SECONDS,
                **POSTGRESQL_KEEPALIVES,
            },
            "pool_pre_ping": True,
            "pool_recycle": POSTGRESQL_POOL_RECYCLE_SECONDS,
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
        }
    return {}


settings = get_settings()
_sqlite_file = False
if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.removeprefix("sqlite:///")
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _sqlite_file = True
engine = create_engine(settings.database_url, **engine_options(settings.database_url, settings))


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


@dataclass(frozen=True)
class DatabaseServer:
    """What a PostgreSQL connection says about the server and how it was
    reached: `host` is where libpq connected (a name, an address, or a Unix
    socket's directory) and `sslmode` the mode it used, from the URL, PGSSLMODE
    or libpq's default (prefer, which checks nothing)."""

    version_num: int
    encoding: str
    host: str
    sslmode: str


class DatabaseServerRefusedError(RuntimeError):
    """The database server can't be used (#306): too old, not UTF8, or
    reached without a verified certificate while that is required."""


def database_server(target: Engine) -> DatabaseServer | None:
    """The PostgreSQL server behind the engine, or None for SQLite."""
    if target.dialect.name != "postgresql":
        return None
    with target.connect() as connection:
        raw = connection.connection.dbapi_connection
        info = raw.info
        sslmode = next(
            (
                option.val.decode()
                for option in raw.pgconn.info
                if option.keyword == b"sslmode" and option.val
            ),
            "prefer",
        )
        return DatabaseServer(
            version_num=info.server_version,
            encoding=info.parameter_status("server_encoding") or "",
            host=info.host or "",
            sslmode=sslmode,
        )


def is_local_host(host: str) -> bool:
    """A Unix socket (a directory, or an abstract socket starting with @),
    localhost or a loopback address: a connection that never leaves this
    host, so TLS can't protect anything there."""
    if not host or host.startswith(("/", "@")) or host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# Whether this process runs on a connection that doesn't verify the server's
# certificate (DATABASE_REQUIRE_VERIFIED_TLS=false), for the warning at the
# top of the system settings pages (app/api/system_settings.py).
_tls_not_verified = False


def database_tls_not_verified() -> bool:
    return _tls_not_verified


TLS_ADVICE = (
    "Add sslmode=verify-full and sslrootcert=<the CA's certificate> to DATABASE_URL "
    "(see docs/configuration.md), then remove DATABASE_REQUIRE_VERIFIED_TLS=false."
)


def check_database_server(target: Engine, log, settings: Settings | None = None) -> None:
    """Refuses a PostgreSQL server older than 18 or not in UTF8, and one
    reached over the network without a verified certificate unless
    DATABASE_REQUIRE_VERIFIED_TLS is false, which only warns. Each refusal
    logs a CRITICAL event (docs/monitoring.md) and raises
    DatabaseServerRefusedError. Nothing to check on SQLite."""
    global _tls_not_verified
    settings = settings or get_settings()
    server = database_server(target)
    if server is None:
        return
    if server.version_num < POSTGRESQL_MIN_SERVER_VERSION:
        log.critical(
            "startup.database_version_unsupported",
            server_version_num=server.version_num,
            minimum_version_num=POSTGRESQL_MIN_SERVER_VERSION,
            hint="PerchTail needs PostgreSQL 18 or later",
        )
        raise DatabaseServerRefusedError(
            f"the PostgreSQL server is version {server.version_num}; PerchTail needs "
            f"{POSTGRESQL_MIN_SERVER_VERSION} (PostgreSQL 18) or later -- refusing to start."
        )
    if server.encoding != "UTF8":
        log.critical(
            "startup.database_encoding_unsupported",
            server_encoding=server.encoding,
            hint="create the database with ENCODING 'UTF8' (see docs/configuration.md)",
        )
        raise DatabaseServerRefusedError(
            f"the PostgreSQL database's encoding is {server.encoding}; PerchTail needs UTF8 "
            "-- refusing to start. Create the database with ENCODING 'UTF8'."
        )
    unverified = server.sslmode not in VERIFIED_SSLMODES and not is_local_host(server.host)
    if unverified and settings.database_require_verified_tls:
        log.critical(
            "startup.database_tls_not_verified",
            host=server.host,
            sslmode=server.sslmode,
            hint=TLS_ADVICE,
        )
        raise DatabaseServerRefusedError(
            f"the database on {server.host} is reached with sslmode={server.sslmode}, which "
            "doesn't verify the server's certificate, and DATABASE_REQUIRE_VERIFIED_TLS is "
            "true -- refusing to start. " + TLS_ADVICE
        )
    if unverified:
        log.warning(
            "startup.database_tls_not_verified",
            host=server.host,
            sslmode=server.sslmode,
            hint="DATABASE_REQUIRE_VERIFIED_TLS is false: only on a private network. " + TLS_ADVICE,
        )
    _tls_not_verified = unverified


def init_db() -> None:
    from app.name_index_store import ensure_name_index_schema

    SQLModel.metadata.create_all(engine)
    ensure_name_index_schema(engine)


def get_session():
    with Session(engine) as session:
        yield session
