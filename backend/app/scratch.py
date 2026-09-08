import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ScratchEntry:
    key: str
    path: Path
    refcount: int = 0
    last_accessed: float = field(default_factory=time.monotonic)


class ScratchStore:
    """Ephemeral per-session fetch cache (CLAUDE.md's "Live browsing &
    ephemeral fetch behavior"). Bookkeeping is in-memory only, not in
    SQLite — scratch content is inherently transient and doesn't need to
    survive a restart, and a restart would drop any live viewer sessions
    referencing it anyway."""

    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes
        self._entries: dict[str, ScratchEntry] = {}
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        return self.root / key

    def acquire(self, key: str) -> Path:
        """Marks key as in-use (refcount += 1) and returns its scratch path.
        Every open is a fresh fetch — the caller must (re)write the file's
        content into this path itself; acquire() never reuses a prior
        fetch's bytes even if the path already exists from a still-open
        session."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = ScratchEntry(key=key, path=self.path_for(key))
                self._entries[key] = entry
            entry.refcount += 1
            entry.last_accessed = time.monotonic()
            return entry.path

    def release(self, key: str) -> None:
        """Reference-counted purge — delete only when the last viewer closes
        it."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.refcount = max(0, entry.refcount - 1)
            if entry.refcount == 0:
                self._delete(entry)

    def touch(self, key: str) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_accessed = time.monotonic()

    def _delete(self, entry: ScratchEntry) -> None:
        entry.path.unlink(missing_ok=True)
        del self._entries[entry.key]
        logger.info("scratch.purge", key=entry.key)

    def sweep_idle(self, idle_seconds: float) -> int:
        """Backstop for crashed/disconnected clients that never send a close
        signal — deletes anything untouched past idle_seconds, regardless of
        refcount, since a vanished client isn't coming back to release it."""
        now = time.monotonic()
        with self._lock:
            stale = [e for e in self._entries.values() if now - e.last_accessed > idle_seconds]
            for entry in stale:
                self._delete(entry)
        if stale:
            logger.warning("scratch.idle_sweep", purged=len(stale))
        return len(stale)

    def total_bytes(self) -> int:
        """Current total scratch usage -- exposed for the detailed health
        endpoint (app/api/monitoring.py) to report usage against
        max_bytes, same computation enforce_size_guard already does
        internally."""
        with self._lock:
            return sum(e.path.stat().st_size for e in self._entries.values() if e.path.exists())

    def enforce_size_guard(self) -> int:
        """A safety valve for load, not a caching strategy: evicts the
        oldest zero-reference entries first once total scratch usage passes
        max_bytes. Never touches an entry with refcount > 0, so a file a
        viewer currently has open is never pulled out from under them."""
        with self._lock:
            total = sum(e.path.stat().st_size for e in self._entries.values() if e.path.exists())
            if total <= self.max_bytes:
                return 0

            candidates = sorted(
                (e for e in self._entries.values() if e.refcount == 0),
                key=lambda e: e.last_accessed,
            )
            evicted = 0
            for entry in candidates:
                if total <= self.max_bytes:
                    break
                size = entry.path.stat().st_size if entry.path.exists() else 0
                self._delete(entry)
                total -= size
                evicted += 1
        if evicted:
            logger.warning("scratch.size_guard_eviction", evicted=evicted)
        return evicted


@lru_cache
def get_scratch_store() -> ScratchStore:
    settings = get_settings()
    max_bytes = int(settings.scratch_max_gb * 1024**3)
    return ScratchStore(root=Path(settings.scratch_dir), max_bytes=max_bytes)
