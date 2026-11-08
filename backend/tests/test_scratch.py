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


def test_concurrent_opens_of_the_same_file_never_share_bytes(tmp_path):
    # Two viewers opening the same file at once each get their own fresh
    # fetch: the second fetch must not truncate or rewrite the file the
    # first response may still be streaming from.
    store = _store(tmp_path)
    first = store.acquire("key1")
    first.write_text("first fetch")
    second = store.acquire("key1")
    second.write_text("second, longer fetch")

    assert first != second
    assert first.read_text() == "first fetch"

    store.release("key1")
    store.release("key1")
    assert not first.exists()
    assert not second.exists()


def test_size_accounting_counts_every_fetch_of_a_key(tmp_path):
    store = _store(tmp_path, max_bytes=1000)
    store.acquire("key1").write_bytes(b"x" * 10)
    store.acquire("key1").write_bytes(b"x" * 20)
    assert store.total_bytes() == 30


# --- #329: leftovers and usage on disk ----------------------------------------


def test_clear_leftovers_deletes_everything_in_the_scratch_directory(tmp_path):
    store = _store(tmp_path)
    (store.root / "abc-123").write_bytes(b"x" * 10)
    leftover_dir = store.root / "perchtail-zip-1"
    leftover_dir.mkdir()
    (leftover_dir / "download.zip").write_bytes(b"y")

    assert store.clear_leftovers(protected=[tmp_path / "db"]) == 2
    assert list(store.root.iterdir()) == []


def test_clear_leftovers_refuses_a_directory_holding_the_database(tmp_path):
    store = ScratchStore(root=tmp_path, max_bytes=1)
    database = tmp_path / "db"
    database.mkdir()
    (database / "perchtail.db").write_bytes(b"data")

    assert store.clear_leftovers(protected=[database]) == 0
    assert (database / "perchtail.db").exists()


def test_usage_counts_files_on_disk_not_only_tracked_ones(tmp_path):
    store = _store(tmp_path)
    store.acquire("key1").write_bytes(b"a" * 100)
    untracked = store.root / "perchtail-zip-1"
    untracked.mkdir()
    (untracked / "download.zip").write_bytes(b"b" * 50)

    assert store.total_bytes() == 150


def test_the_size_guard_counts_untracked_files_too(tmp_path):
    store = _store(tmp_path, max_bytes=120)
    path = store.acquire("key1")
    path.write_bytes(b"a" * 100)
    store._entries["key1"].refcount = 0  # as in the eviction tests above
    store._entries["key1"].last_accessed = time.monotonic() - 100
    other = store.acquire("key2")
    other.write_bytes(b"c" * 10)
    store._entries["key2"].refcount = 0
    # Tracked entries alone (110 bytes) are under the cap; a ZIP being built
    # (not tracked by the store, still on its disk) takes it over.
    (store.root / "perchtail-zip-1").mkdir()
    (store.root / "perchtail-zip-1" / "download.zip").write_bytes(b"b" * 50)

    assert store.enforce_size_guard() == 1
    assert not path.exists()
    assert other.exists()


def test_remote_copies_live_under_the_scratch_directory(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.scratch import make_scratch_dir, scratch_temp_dir

    monkeypatch.setattr(get_settings(), "scratch_dir", str(tmp_path / "scratch"))
    with scratch_temp_dir() as tmp:
        assert tmp.parent == tmp_path / "scratch"
        (tmp / "fetched").write_bytes(b"x")
    assert not tmp.exists()
    made = make_scratch_dir("perchtail-zip-")
    assert made.parent == tmp_path / "scratch"
