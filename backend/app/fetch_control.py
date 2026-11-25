"""Progress and Cancel for a file opened whole (/open): how much of it has
been copied from the source, and a way to stop that copy.

The viewer names each open with a random `request_id` (32 hex characters).
/open registers a FetchControl under (user id, request id) for as long as
it runs and puts it in `current` around the fetch; the connectors' copy
loops, which run in the same thread (TranslatingConnector.call), count
what they wrote into it and, between blocks, stop with FetchCancelledError
once Cancel set its event. GET /open-progress reads it (the viewer asks
about all its opens in one request), POST /open-cancel sets the event. In
memory only: a restart forgets every control, as it ends every request.

Without a control in `current` (a download, a folder ZIP, Tail, an /open
without request_id), nothing changes: the helpers below cost one
ContextVar.get."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import BinaryIO, Literal

from app.collectors.base import FetchCancelledError

# The most opens with progress one user may have at once; a ninth answers
# as before, without progress.
MAX_PER_USER = 8

REQUEST_ID_PATTERN = r"^[0-9a-f]{32}$"

Phase = Literal["copying", "redacting"]


@dataclass
class FetchControl:
    """One /open in progress: what it expects (bytes, when the connector
    knows the size), what it wrote, its phase, and its Cancel."""

    user_id: int
    source_id: int
    expected: int | None = None
    written: int = 0
    phase: Phase = "copying"
    cancel: threading.Event = field(default_factory=threading.Event)

    def check(self) -> None:
        """Stops the copy (FetchCancelledError) once Cancel was asked."""
        if self.cancel.is_set():
            raise FetchCancelledError()


_controls: dict[tuple[int, str], FetchControl] = {}
_lock = threading.Lock()

current: ContextVar[FetchControl | None] = ContextVar("fetch_control", default=None)


def _register(user_id: int, source_id: int, request_id: str) -> FetchControl | None:
    with _lock:
        if (user_id, request_id) in _controls:
            return None
        if sum(1 for uid, _ in _controls if uid == user_id) >= MAX_PER_USER:
            return None
        control = FetchControl(user_id=user_id, source_id=source_id)
        _controls[(user_id, request_id)] = control
        return control


def find(user_id: int, source_id: int, request_id: str) -> FetchControl | None:
    """The user's control for `request_id` on that source, or None (none,
    finished, another user's or another source's)."""
    control = find_own(user_id, request_id)
    if control is None or control.source_id != source_id:
        return None
    return control


def find_own(user_id: int, request_id: str) -> FetchControl | None:
    """The user's control for `request_id` on any source, or None (none,
    finished or another user's)."""
    with _lock:
        return _controls.get((user_id, request_id))


@contextmanager
def controlling(user_id: int, source_id: int, request_id: str | None) -> Iterator[None]:
    """Registers a control for this /open and puts it in `current` while
    the block runs; removed when the block ends, however it ends. Without
    a request id, or over MAX_PER_USER, the block runs without one."""
    control = _register(user_id, source_id, request_id) if request_id else None
    if control is None:
        yield
        return
    token = current.set(control)
    try:
        yield
    finally:
        current.reset(token)
        with _lock:
            _controls.pop((user_id, request_id), None)


def clear() -> None:
    """For tests."""
    with _lock:
        _controls.clear()


# -- what the connectors call ------------------------------------------------


def expect(size: int | None) -> None:
    """The size the copy will write, from the stat the connector made."""
    control = current.get()
    if control is not None and size is not None and size >= 0:
        control.expected = size


def wrote(count: int) -> None:
    """`count` more bytes written; stops the copy when Cancel was asked."""
    control = current.get()
    if control is not None:
        control.written += count
        control.check()


def check() -> None:
    """Between blocks of a copy: stops it when Cancel was asked."""
    control = current.get()
    if control is not None:
        control.check()


def redacting() -> FetchControl | None:
    """The copy is done and redaction starts; the control, if any."""
    control = current.get()
    if control is not None:
        control.phase = "redacting"
    return control


class CountingSink:
    """A push agent's chunks written for an /open (agent_registry writes
    them on the event loop's threads, where `current` isn't set): counts
    them, and once Cancel was asked refuses the next one with a ValueError,
    which ends the command there and sends the agent `cancel`, as a
    timeout does. raise_if_cancelled then turns the failure into
    FetchCancelledError."""

    def __init__(self, sink: BinaryIO, control: FetchControl):
        self._sink = sink
        self._control = control

    def write(self, data: bytes) -> int:
        if self._control.cancel.is_set():
            raise ValueError("the read was cancelled")
        written = self._sink.write(data)
        self._control.written += len(data)
        return written


def counting(sink: BinaryIO) -> BinaryIO:
    """`sink`, counted for the /open in progress (CountingSink), or as is."""
    control = current.get()
    return sink if control is None else CountingSink(sink, control)  # type: ignore[return-value]


def raise_if_cancelled(exc: BaseException) -> None:
    """Re-raises a copy that failed because Cancel was asked as
    FetchCancelledError."""
    control = current.get()
    if control is not None and control.cancel.is_set() and not isinstance(exc, FetchCancelledError):
        raise FetchCancelledError() from exc


class CancellableReader:
    """A file read by redaction that stops it (FetchCancelledError) once
    Cancel was asked, checked before each read."""

    def __init__(self, src: BinaryIO, control: FetchControl):
        self._src = src
        self._control = control

    def read(self, size: int = -1) -> bytes:
        self._control.check()
        return self._src.read(size)

    def __getattr__(self, name: str):
        return getattr(self._src, name)
