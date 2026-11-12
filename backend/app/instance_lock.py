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

High availability with several instances comes with PostgreSQL (#306)."""

import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.version import APP_VERSION

LOCK_FILE_NAME = ".perchtail.lock"

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
