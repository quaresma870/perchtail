"""One PerchTail process per DATA_DIR (#334).

Two instances on the same DATA_DIR would both run the scheduler, both
migrate the database and both write to SQLite, which isn't built for it.
Startup (app/main.py's lifespan) and `alembic upgrade` (migrations/env.py)
take an exclusive, non-blocking lock from the operating system on
`DATA_DIR/.perchtail.lock` before touching the database, and refuse to go
on when another process holds it.

The lock belongs to the process: when it ends, however it ends (SIGKILL,
a crash, a killed container), the operating system releases it, so there is
never a stale lock to clean up and no setting to turn the guard off. The
file itself stays; only the lock on it matters. While held, it says which
process holds it (host, PID, version and when it took the lock), so the
refusal can name it.

Running several instances side by side, for high availability or load, is
out of scope: they would share one SQLite database.

On PostgreSQL (#306) two instances with different DATA_DIRs (two containers
with their own volumes) could still share one database, so both also take a
session-level advisory lock in that database (acquire_database), held on a
connection of its own until shutdown. PostgreSQL releases it when that
connection ends, however the process ends. Connections send TCP keepalives
(app/db.py) and the database_lock_check job (DatabaseLock.check) confirms
every minute that the lock is still held, taking it again when it was lost."""

import contextlib
import json
import os
import socket
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.version import APP_VERSION

logger = get_logger(__name__)

LOCK_FILE_NAME = ".perchtail.lock"
# The advisory lock's key in a PostgreSQL database: "PERCHTLK" in ASCII, a
# constant no other application is likely to pick.
DATABASE_LOCK_KEY = 0x50455243_4854_4C4B

# Windows locks are mandatory: a locked byte range can't be read by another
# process. The lock sits on one byte far past the holder's details, so the
# refused process can still read them. POSIX flock locks the whole file but
# is advisory, so reading works there anyway.
_WINDOWS_LOCK_OFFSET = 1 << 20

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceLockHeldError(RuntimeError):
    """Another process holds the lock. `content` is what the lock file says
    about it; `holder` is that content parsed (empty when it couldn't be,
    e.g. read while the holder was still writing it)."""

    def __init__(self, path: Path, content: str):
        self.path = path
        self.content = content
        self.holder = _parse(content)
        who = ", ".join(f"{key}={value}" for key, value in self.holder.items()) or content
        super().__init__(
            f"another PerchTail instance is using DATA_DIR ({path.parent}): {who or 'unknown'}. "
            "Only one instance may run per DATA_DIR; stop the other one first."
        )


def _parse(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class InstanceLock:
    """A held lock, until release()."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self._fd: int | None = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Unlocks and closes the file. Safe to call twice. The file is left
        in place: removing it could let a second process lock a file the
        next one no longer sees."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass  # closing the file releases it anyway
        finally:
            os.close(fd)


def lock_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return Path(settings.data_dir) / LOCK_FILE_NAME


def acquire(settings: Settings | None = None) -> InstanceLock:
    """Takes the lock for this process, or raises InstanceLockHeldError."""
    path = lock_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _try_lock(fd)
        except OSError:
            os.lseek(fd, 0, os.SEEK_SET)
            content = os.read(fd, 64 * 1024).decode("utf-8", errors="replace").strip("\0 \n")
            raise InstanceLockHeldError(path, content) from None
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        details = {
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "version": APP_VERSION,
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (json.dumps(details) + "\n").encode())
    except BaseException:
        os.close(fd)
        raise
    return InstanceLock(path, fd)


class DatabaseLockHeldError(RuntimeError):
    """Another session holds the advisory lock in the PostgreSQL database.
    `holder` says what pg_stat_activity shows of it (empty when it can't be
    seen)."""

    def __init__(self, holder: dict[str, Any]):
        self.holder = holder
        who = ", ".join(f"{key}={value}" for key, value in holder.items()) or "unknown"
        super().__init__(
            f"another PerchTail instance is using this database: {who}. "
            "Only one instance may run per database; stop the other one first."
        )


_lock_lost = False


def database_lock_lost() -> bool:
    """True once another process took the database lock from this one
    (DatabaseLock.check): GET /readyz fails from then until the process ends."""
    return _lock_lost


def _discard(connection: Connection) -> None:
    try:
        # Whatever invalidating a dead connection raises, it is being thrown away.
        with contextlib.suppress(Exception):
            connection.invalidate()
    finally:
        connection.close()


class DatabaseLock:
    """A held advisory lock, until release()."""

    def __init__(self, connection: Connection, engine: Engine):
        self._connection: Connection | None = connection
        self._engine = engine
        # The scheduler's check and the lifespan's release never overlap.
        self._guard = threading.Lock()

    @property
    def held(self) -> bool:
        return self._connection is not None

    def release(self) -> None:
        """Unlocks and closes the connection. Safe to call twice; closing
        the connection releases the lock anyway."""
        with self._guard:
            connection, self._connection = self._connection, None
            if connection is None:
                return
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": DATABASE_LOCK_KEY}
                )
                connection.commit()
            except Exception:  # the connection may be gone already
                connection.invalidate()
            finally:
                connection.close()

    def _still_held(self) -> bool:
        connection = self._connection
        if connection is None:
            return False
        try:
            held = connection.execute(
                _STILL_HELD,
                {"high": DATABASE_LOCK_KEY >> 32, "low": DATABASE_LOCK_KEY & 0xFFFFFFFF},
            ).scalar()
            connection.commit()
        except Exception:  # noqa: BLE001 - a dropped connection lost the lock
            return False
        return bool(held)

    def check(self) -> None:
        """The database_lock_check job: confirms the lock's connection still
        holds it. A server restart, a failover or a cut idle connection ends
        the session and its lock while the pool reconnects everything else;
        then it takes the lock again on a new connection (WARNING
        database.instance_lock_reacquired), or, when another process has
        taken it meanwhile, logs CRITICAL database.instance_lock_lost and
        keeps GET /readyz failing until the process ends. An unreachable
        server raises, so the job's run is recorded as failed and it tries
        again next time."""
        global _lock_lost
        with self._guard:
            if _lock_lost or self._still_held():
                return
            if self._connection is not None:
                _discard(self._connection)
                self._connection = None
            try:
                fresh = acquire_database(self._engine)
            except DatabaseLockHeldError as exc:
                _lock_lost = True
                holder = exc.holder
                logger.critical(
                    "database.instance_lock_lost",
                    holder_host=holder.get("host"),
                    holder_pid=holder.get("pid"),
                    holder_started_at=holder.get("started_at"),
                    hint="another PerchTail instance took this database's lock; "
                    "this one is not ready until it is stopped",
                )
                return
            self._connection, fresh._connection = fresh._connection, None
            logger.warning(
                "database.instance_lock_reacquired",
                hint="the connection holding the database lock was lost and the lock taken again",
            )


_HOLDER = text(
    "SELECT a.pid, host(a.client_addr) AS client_addr, a.backend_start "
    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1 "
    "AND l.classid = :high AND l.objid = :low AND l.database = "
    "(SELECT oid FROM pg_database WHERE datname = current_database()) LIMIT 1"
)
_STILL_HELD = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND objsubid = 1 AND classid = :high AND objid = :low AND pid = pg_backend_pid())"
)


def _database_lock_holder(connection: Connection) -> dict[str, Any]:
    row = connection.execute(
        _HOLDER,
        {"high": DATABASE_LOCK_KEY >> 32, "low": DATABASE_LOCK_KEY & 0xFFFFFFFF},
    ).first()
    if row is None:
        return {}
    return {
        "host": row.client_addr or "local socket",
        "pid": row.pid,
        "started_at": row.backend_start.isoformat(timespec="seconds"),
    }


def acquire_database(target: Engine) -> DatabaseLock | None:
    """Takes the advisory lock in a PostgreSQL database, or raises
    DatabaseLockHeldError. None on SQLite, where the file lock is enough."""
    if target.dialect.name != "postgresql":
        return None
    connection = target.connect()
    try:
        taken = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": DATABASE_LOCK_KEY}
        ).scalar()
        holder = {} if taken else _database_lock_holder(connection)
        # The lock belongs to the session, not the transaction: ending it
        # keeps the connection from sitting idle in a transaction.
        connection.commit()
    except BaseException:
        connection.close()
        raise
    if not taken:
        connection.close()
        raise DatabaseLockHeldError(holder)
    return DatabaseLock(connection, target)


DATABASE_LOCK_CHECK_INTERVAL = timedelta(minutes=1)


def register_database_lock_check(scheduler, lock: DatabaseLock | None) -> None:
    """The database_lock_check job, only when a database lock is held
    (PostgreSQL)."""
    if lock is None:
        return
    from app.jobs import register  # deferred: app.jobs imports the models

    register(
        scheduler,
        "database_lock_check",
        "Database instance lock check",
        lock.check,
        DATABASE_LOCK_CHECK_INTERVAL,
    )


def log_database_held(logger, error: DatabaseLockHeldError) -> None:
    """The same CRITICAL event as the file lock's, naming the database."""
    holder = error.holder
    logger.critical(
        "startup.instance_lock_held",
        lock="database",
        holder_host=holder.get("host"),
        holder_pid=holder.get("pid"),
        holder_started_at=holder.get("started_at"),
        hint="another PerchTail instance is using this database; only one may run per database",
    )


def log_held(logger, error: InstanceLockHeldError) -> None:
    """The CRITICAL event every refusal logs (docs/monitoring.md)."""
    holder = error.holder
    logger.critical(
        "startup.instance_lock_held",
        lock_file=str(error.path),
        holder_host=holder.get("host"),
        holder_pid=holder.get("pid"),
        holder_version=holder.get("version"),
        holder_started_at=holder.get("started_at"),
        hint="another PerchTail instance is using this DATA_DIR; only one may run per DATA_DIR",
    )
