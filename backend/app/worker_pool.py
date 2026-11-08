"""A thread pool that can be drained at shutdown (#335): it stops taking
work, drops what's still queued and waits, up to a time limit, for what's
running. The name index and the background source check use one each."""

import threading
from concurrent.futures import Future, ThreadPoolExecutor


class DrainablePool:
    def __init__(self, workers: int, name: str):
        self.name = name
        self.workers = workers
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._cond = threading.Condition()
        # Queued plus running; a cancelled item also runs its done callback.
        self._outstanding = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def submit(self, fn, *args, **kwargs) -> Future | None:
        """None once the pool is draining: the caller drops the work."""
        with self._cond:
            if self._closed:
                return None
            self._outstanding += 1
        try:
            future = self._executor.submit(fn, *args, **kwargs)
        except RuntimeError:  # shut down between the check and the submit
            self._done(None)
            return None
        future.add_done_callback(self._done)
        return future

    def _done(self, _future) -> None:
        with self._cond:
            self._outstanding -= 1
            self._cond.notify_all()

    def drain(self, timeout: float) -> bool:
        """True when everything running finished within `timeout` seconds."""
        with self._cond:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        with self._cond:
            return self._cond.wait_for(lambda: self._outstanding <= 0, timeout=max(timeout, 0))
