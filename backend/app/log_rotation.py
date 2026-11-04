"""Rotation and retention for the app's own log file (#172).

The active file rotates when it reaches LOG_MAX_FILE_MB or at midnight,
whichever comes first. Each rotated file is gzipped as
`perchtail.log.<date>.<n>.gz`, `<date>` being the day its lines were written
and `<n>` counting the rotations that day, so a verbose day can rotate
several times without overwriting anything.

Archives are deleted when they are older than LOG_RETENTION_DAYS, and the
oldest ones go first whenever all logs together exceed LOG_MAX_TOTAL_MB.
When the size limit removes an archive the day limit would still have kept
(typically DEBUG left on), that is logged as a warning and reported by
/monitoring/health, so the lost history doesn't go unnoticed.

Archives written by earlier versions (`perchtail.log.<date>.gz`, one per
day) are recognised and expire under the same rules."""

import gzip
import logging
import logging.handlers
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

_MB = 1024 * 1024


@dataclass
class RetentionPressure:
    """Set when the total-size limit deleted logs younger than the day limit."""

    deleted_files: int = 0
    newest_deleted_day: str | None = None
    last_at: float | None = None


_pressure = RetentionPressure()
_pressure_lock = threading.Lock()


def retention_pressure() -> RetentionPressure:
    with _pressure_lock:
        return RetentionPressure(**vars(_pressure))


def reset_retention_pressure() -> None:
    global _pressure
    with _pressure_lock:
        _pressure = RetentionPressure()


def _archive_re(base_name: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(base_name)}\.(\d{{4}}-\d{{2}}-\d{{2}})(?:\.(\d+))?\.gz$")


class SizeAndTimeRotatingFileHandler(logging.handlers.BaseRotatingHandler):
    def __init__(
        self,
        filename: str,
        *,
        max_bytes: int,
        retention_days: int,
        max_total_bytes: int,
        encoding: str = "utf-8",
        clock=time.time,
    ):
        super().__init__(filename, mode="a", encoding=encoding, delay=False)
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self.max_total_bytes = max_total_bytes
        self._clock = clock
        self._dir = Path(self.baseFilename).parent
        self._base = Path(self.baseFilename).name
        self._archive_re = _archive_re(self._base)
        # The day the active file's lines belong to: its last write, so a
        # file left over from before a restart is archived under its own day.
        existing = Path(self.baseFilename)
        started = (
            existing.stat().st_mtime if existing.exists() and existing.stat().st_size else clock()
        )
        self._period_day = date.fromtimestamp(started)
        self._rollover_at = self._next_midnight(started)
        self._logger = logging.getLogger("app.log_rotation")

    @staticmethod
    def _next_midnight(timestamp: float) -> float:
        day = date.fromtimestamp(timestamp) + timedelta(days=1)
        return datetime(day.year, day.month, day.day).timestamp()

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self._clock() >= self._rollover_at:
            return True
        if self.max_bytes > 0 and self.stream is not None:
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() >= self.max_bytes:
                return True
        return False

    def archives(self) -> list[tuple[date, int, Path]]:
        """Rotated files, oldest first."""
        out = []
        for path in self._dir.iterdir():
            match = self._archive_re.match(path.name)
            if match:
                day = date.fromisoformat(match.group(1))
                out.append((day, int(match.group(2) or 0), path))
        out.sort(key=lambda a: (a[0], a[1]))
        return out

    def _next_archive_path(self, day: date) -> Path:
        used = [seq for d, seq, _ in self.archives() if d == day]
        seq = max(used, default=0) + 1
        return self._dir / f"{self._base}.{day.isoformat()}.{seq}.gz"

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        source = Path(self.baseFilename)
        if source.exists() and source.stat().st_size > 0:
            dest = self._next_archive_path(self._period_day)
            with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            source.unlink()
        now = self._clock()
        self._period_day = date.fromtimestamp(now)
        self._rollover_at = self._next_midnight(now)
        self.stream = self._open()
        self.enforce_retention()

    def enforce_retention(self) -> None:
        today = date.fromtimestamp(self._clock())
        cutoff = today - timedelta(days=self.retention_days)
        archives = self.archives()
        kept = []
        for day, seq, path in archives:
            if self.retention_days > 0 and day < cutoff:
                path.unlink(missing_ok=True)
            else:
                kept.append((day, seq, path))

        if self.max_total_bytes <= 0:
            return
        active = Path(self.baseFilename)
        total = (active.stat().st_size if active.exists() else 0) + sum(
            p.stat().st_size for _, _, p in kept
        )
        early: list[date] = []
        while kept and total > self.max_total_bytes:
            day, _, path = kept.pop(0)
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
            early.append(day)
        if early:
            with _pressure_lock:
                _pressure.deleted_files += len(early)
                newest = max(early).isoformat()
                if _pressure.newest_deleted_day is None or newest > _pressure.newest_deleted_day:
                    _pressure.newest_deleted_day = newest
                _pressure.last_at = self._clock()
            # Logged after the new file is open (doRollover), so this lands
            # in the fresh file rather than the one just archived.
            self._logger.warning(
                "logs.retention.size_limit: deleted %d log archive(s) up to %s to stay under "
                "LOG_MAX_TOTAL_MB=%d, before LOG_RETENTION_DAYS=%d would have; consider a lower "
                "LOG_LEVEL or LOG_QUIET_THIRD_PARTY=true",
                len(early),
                max(early).isoformat(),
                self.max_total_bytes // _MB,
                self.retention_days,
            )


NOISY_THIRD_PARTY_LOGGERS = (
    "smbprotocol",
    "spnego",
    "paramiko",
    "urllib3",
    "requests_ntlm",
    "winrm",
)
