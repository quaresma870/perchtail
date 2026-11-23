"""Ranged reads of a remote file, for archives that can be read in place.

A `.zip` keeps its index (the central directory) at its end and each
member's data in one stretch, so zipfile only needs the end of the archive
to list it and one member's stretch to extract it. Over SSH, SMB and WinRM,
the connector's `open_ranged` opens the file for the length of one
operation and yields a RangedRemote; RangedFile wraps it as a read-only,
seekable file object zipfile runs on, without copying the archive to
scratch:

- reads outside a member are served from a cache of BLOCK_BYTES-aligned
  blocks (CACHE_BLOCKS of them, least recently used out first), so the
  end-of-central-directory record, the ZIP64 locator and the central
  directory usually cost one or two remote reads;
- reads inside the stretch a member's extraction announced
  (`read_sequentially`) are read ahead, READAHEAD_START bytes at first and
  twice as much on each read that follows the last, up to READAHEAD_MAX, and
  never past the stretch.

Every remote read counts against the operation's limit (MAX_FETCH_MB):
what is transferred, not the archive's size. The connector checks on every
read that the file still has the size it had when it was opened and raises
RemoteFileChangedError otherwise, so data from two versions of a file is
never put together."""

import io
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from app.collectors.base import too_large
from app.logging_config import get_logger

logger = get_logger(__name__)

BLOCK_BYTES = 1024 * 1024
CACHE_BLOCKS = 4
READAHEAD_START = 1024 * 1024
READAHEAD_MAX = 8 * 1024 * 1024
# A remote read longer than this goes through copy_range (streamed) rather
# than read_at (one answer).
_READ_AT_MAX = 2 * BLOCK_BYTES


class RemoteFileChangedError(Exception):
    """The remote file doesn't have the size it had when it was opened, or
    ended before it: it was written to, rotated or replaced while it was
    read. Nothing read from it is used; the archive routes refuse it as an
    archive that can't be read (409 archive_unreadable)."""


class RangedRemote(Protocol):
    """What a connector's open_ranged yields, for one operation: the file's
    size when it was opened, and reads at an offset that check it hasn't
    changed since (RemoteFileChangedError)."""

    size: int

    def read_at(self, offset: int, length: int) -> bytes:
        """Exactly `length` bytes from `offset` (inside the file)."""
        ...

    def copy_range(self, offset: int, length: int, dst: BinaryIO) -> int:
        """Exactly `length` bytes from `offset` written to `dst` as they
        arrive; returns how many."""
        ...


def check_size(expected: int, current: int) -> None:
    """For the connectors: RemoteFileChangedError when the file's size is
    no longer the one it had when it was opened."""
    if current != expected:
        raise RemoteFileChangedError(f"the file's size went from {expected} to {current} bytes")


def check_length(expected: int, received: int) -> None:
    """For the connectors: RemoteFileChangedError when a read got fewer (or
    more) bytes than asked inside the file: it ended early."""
    if received != expected:
        raise RemoteFileChangedError(f"a read of {expected} bytes got {received}")


@dataclass
class RangedStats:
    """What an operation read: remote reads, bytes transferred and the
    seconds spent waiting for them."""

    round_trips: int = 0
    bytes: int = 0
    seconds: float = 0.0


class RangedFile(io.RawIOBase):
    """A read-only, seekable file over a RangedRemote (see the module
    docstring). `limit` is the most bytes the operation may transfer (None:
    no limit); a read that would need more raises FetchTooLargeError."""

    def __init__(self, remote: RangedRemote, *, limit: int | None = None):
        super().__init__()
        self._remote = remote
        self._size = remote.size
        self._limit = limit
        self._pos = 0
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        # The stretch read ahead, [start, end), and what was read of it.
        self._window: tuple[int, int] | None = None
        self._ahead_start = 0
        self._ahead = b""
        self._ahead_next = READAHEAD_START
        self.stats = RangedStats()

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._pos + offset
        elif whence == io.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if position < 0:
            raise OSError("negative seek position")
        self._pos = position
        return position

    def readinto(self, buffer) -> int:
        wanted = min(len(buffer), self._size - self._pos)
        if wanted <= 0:
            return 0
        data = self._read(self._pos, wanted)
        buffer[: len(data)] = data
        self._pos += len(data)
        return len(data)

    def readall(self) -> bytes:
        wanted = self._size - self._pos
        if wanted <= 0:
            return b""
        data = self._read(self._pos, wanted)
        self._pos += len(data)
        return data

    def read_sequentially(self, start: int, end: int) -> None:
        """Announces that [start, end) is about to be read front to back (a
        member's local header and data): reads inside it are read ahead,
        never past `end`. Refuses up front (FetchTooLargeError) a stretch
        the operation's limit can't cover."""
        start, end = max(start, 0), min(end, self._size)
        if self._limit is not None and self.stats.bytes + (end - start) > self._limit:
            raise too_large(self.stats.bytes + (end - start), self._limit)
        self._window = (start, end)
        self._ahead_start, self._ahead = start, b""
        self._ahead_next = READAHEAD_START

    def _read(self, position: int, length: int) -> bytes:
        window = self._window
        if window is None or not window[0] <= position < window[1]:
            return self._read_blocks(position, length)
        inside = min(length, window[1] - position)
        data = self._read_ahead(position, inside)
        if inside < length:
            data += self._read_blocks(position + inside, length - inside)
        return data

    def _read_ahead(self, position: int, length: int) -> bytes:
        assert self._window is not None  # nosec B101: only called inside one
        end = self._window[1]
        out = bytearray()
        while length > 0:
            ahead_end = self._ahead_start + len(self._ahead)
            if not self._ahead_start <= position < ahead_end:
                follows = position == ahead_end and bool(self._ahead)
                step = self._ahead_next if follows else READAHEAD_START
                want = min(max(length, step), end - position)
                if self._limit is not None:
                    # Read ahead only what the limit leaves; what is asked
                    # for is read or refused.
                    want = max(length, min(want, self._limit - self.stats.bytes))
                self._ahead = self._fetch(position, want)
                self._ahead_start = position
                self._ahead_next = min(step * 2, READAHEAD_MAX)
            chunk = self._ahead[position - self._ahead_start :][:length]
            out += chunk
            position += len(chunk)
            length -= len(chunk)
        return bytes(out)

    def _read_blocks(self, position: int, length: int) -> bytes:
        first = position // BLOCK_BYTES
        last = (position + length - 1) // BLOCK_BYTES
        found: dict[int, bytes] = {}
        missing: list[int] = []
        for index in range(first, last + 1):
            block = self._blocks.get(index)
            if block is None:
                missing.append(index)
            else:
                self._blocks.move_to_end(index)
                found[index] = block
        for run_start, run_end in _runs(missing):
            offset = run_start * BLOCK_BYTES
            span = min((run_end + 1) * BLOCK_BYTES, self._size) - offset
            if self._limit is not None and self.stats.bytes + span > self._limit:
                # Not enough left for whole blocks: just what was asked.
                lo, hi = max(offset, position), min(offset + span, position + length)
                data = self._fetch(lo, hi - lo)
                return self._assemble(found, position, length, (lo, data))
            data = self._fetch(offset, span)
            for index in range(run_start, run_end + 1):
                start = (index - run_start) * BLOCK_BYTES
                found[index] = data[start : start + BLOCK_BYTES]
                self._remember(index, found[index])
        return self._assemble(found, position, length)

    def _assemble(
        self,
        blocks: dict[int, bytes],
        position: int,
        length: int,
        exact: tuple[int, bytes] | None = None,
    ) -> bytes:
        """The bytes [position, position + length) out of whole `blocks`,
        and out of `exact` (an offset and its bytes) where a block is
        missing."""
        out = bytearray()
        end = position + length
        while position < end:
            index = position // BLOCK_BYTES
            block = blocks.get(index)
            if block is not None:
                start = position - index * BLOCK_BYTES
                chunk = block[start : start + end - position]
            elif exact is not None and exact[0] <= position < exact[0] + len(exact[1]):
                chunk = exact[1][position - exact[0] :][: end - position]
            else:
                chunk = self._fetch(position, min(end, (index + 1) * BLOCK_BYTES) - position)
            if not chunk:
                break
            out += chunk
            position += len(chunk)
        return bytes(out)

    def _remember(self, index: int, block: bytes) -> None:
        self._blocks[index] = block
        self._blocks.move_to_end(index)
        while len(self._blocks) > CACHE_BLOCKS:
            self._blocks.popitem(last=False)

    def _fetch(self, offset: int, length: int) -> bytes:
        length = min(length, self._size - offset)
        if length <= 0:
            return b""
        if self._limit is not None and self.stats.bytes + length > self._limit:
            raise too_large(self.stats.bytes + length, self._limit)
        started = time.monotonic()
        if length <= _READ_AT_MAX:
            data = self._remote.read_at(offset, length)
        else:
            sink = io.BytesIO()
            self._remote.copy_range(offset, length, sink)
            data = sink.getvalue()
        self.stats.seconds += time.monotonic() - started
        self.stats.round_trips += 1
        self.stats.bytes += len(data)
        check_length(length, len(data))
        return data


def _runs(indexes: list[int]) -> Iterator[tuple[int, int]]:
    """Consecutive indexes as (first, last) runs."""
    run: list[int] = []
    for index in indexes:
        if run and index != run[-1] + 1:
            yield run[0], run[-1]
            run = []
        run.append(index)
    if run:
        yield run[0], run[-1]


@contextmanager
def ranged_file(
    remote: RangedRemote, *, limit: int | None, source_id: int | None, operation: str
) -> Iterator[RangedFile]:
    """A RangedFile over `remote` for one operation ("list" or "member"),
    logging what it read when it is done (archive.ranged_read, DEBUG):
    remote reads, bytes and seconds, never a member's name or content."""
    archive_file = RangedFile(remote, limit=limit)
    try:
        yield archive_file
    finally:
        logger.debug(
            "archive.ranged_read",
            source_id=source_id,
            operation=operation,
            round_trips=archive_file.stats.round_trips,
            bytes=archive_file.stats.bytes,
            seconds=round(archive_file.stats.seconds, 3),
            archive_bytes=archive_file.size,
        )
