import time

from app.scratch import ScratchStore


def _store(tmp_path, max_bytes=10_000_000) -> ScratchStore:
    return ScratchStore(root=tmp_path / "scratch", max_bytes=max_bytes)


def test_acquire_creates_the_scratch_directory_and_returns_a_path(tmp_path):
    store = _store(tmp_path)
    path = store.acquire("key1")
    assert path.parent == store.root
    assert store.root.exists()


def test_release_deletes_the_file_when_refcount_hits_zero(tmp_path):
    store = _store(tmp_path)
    path = store.acquire("key1")
    path.write_text("content")

    store.release("key1")
    assert not path.exists()


def test_refcounting_keeps_file_until_last_release(tmp_path):
    store = _store(tmp_path)
    path = store.acquire("key1")
    path.write_text("content")
    store.acquire("key1")  # second viewer opens the same file

    store.release("key1")
    assert path.exists()  # still one reference held

    store.release("key1")
    assert not path.exists()


def test_release_unknown_key_is_a_noop(tmp_path):
    store = _store(tmp_path)
    store.release("never-acquired")  # should not raise


def test_touch_updates_last_accessed(tmp_path):
    store = _store(tmp_path)
    store.acquire("key1")
    entry = store._entries["key1"]
    entry.last_accessed = 0.0

    store.touch("key1")
    assert entry.last_accessed > 0.0


def test_sweep_idle_purges_untouched_entries_regardless_of_refcount(tmp_path):
    store = _store(tmp_path)
    path = store.acquire("key1")
    path.write_text("content")
    store._entries["key1"].last_accessed = time.monotonic() - 1000

    purged = store.sweep_idle(idle_seconds=1)
    assert purged == 1
    assert not path.exists()
    assert "key1" not in store._entries


def test_sweep_idle_leaves_recently_touched_entries(tmp_path):
    store = _store(tmp_path)
    path = store.acquire("key1")
    path.write_text("content")

    purged = store.sweep_idle(idle_seconds=1000)
    assert purged == 0
    assert path.exists()


def test_size_guard_evicts_oldest_zero_reference_entries_first(tmp_path):
    # release() deletes immediately once refcount hits zero (see its
    # docstring), so a zero-ref entry lingering for the size-guard to find
    # isn't reachable through the public API in normal operation — this
    # exercises the eviction algorithm itself (oldest zero-ref first) as a
    # defensive layer, constructing that state directly.
    store = _store(tmp_path, max_bytes=15)
    path_a = store.acquire("a")
    path_a.write_bytes(b"x" * 10)
    store._entries["a"].refcount = 0
    store._entries["a"].last_accessed = time.monotonic() - 100  # oldest

    path_b = store.acquire("b")
    path_b.write_bytes(b"x" * 10)
    store._entries["b"].refcount = 0  # newer

    evicted = store.enforce_size_guard()
    assert evicted == 1
    assert not path_a.exists()
    assert path_b.exists()


def test_size_guard_never_evicts_a_referenced_entry(tmp_path):
    store = _store(tmp_path, max_bytes=5)
    path = store.acquire("a")
    path.write_bytes(b"x" * 100)  # over the cap, but still referenced

    evicted = store.enforce_size_guard()
    assert evicted == 0
    assert path.exists()


def test_size_guard_is_a_noop_under_the_cap(tmp_path):
    store = _store(tmp_path, max_bytes=1000)
    path = store.acquire("a")
    path.write_bytes(b"x" * 10)
    store._entries["a"].refcount = 0

    assert store.enforce_size_guard() == 0
    assert path.exists()
