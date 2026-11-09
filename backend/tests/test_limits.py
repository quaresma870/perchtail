"""Server-side limits (#344), disk guards (#345) and bounded work (#347)."""

import gzip
import io
import os
import time
import zipfile
from collections import namedtuple

import pytest
from app import system_settings
from app.api import archive as archive_api
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.api.errors import install as install_error_handlers
from app.auth.models import Role, User
from app.collectors.base import DirEntry, DirListing, is_truncated
from app.config import get_settings
from app.db import get_session
from app.limits import Limits, SlotLimiter, current_limits, get_limiter
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from app.name_index import MAX_DEPTH
from app.scratch import ScratchFullError, ScratchStore, get_scratch_store
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# --- the limiter itself -----------------------------------------------------


def test_connector_slots_are_limited_globally_and_per_source():
    limiter = SlotLimiter()
    limits = Limits(global_max=3, per_source=2, per_user=1)
    a1 = limiter.connector(limits, 1)
    limiter.connector(limits, 1)
    with pytest.raises(HTTPException) as busy_source:
        limiter.connector(limits, 1)
    assert busy_source.value.status_code == 503
    assert busy_source.value.detail["scope"] == "source"
    assert busy_source.value.headers["Retry-After"] == "5"

    limiter.connector(limits, 2)
    with pytest.raises(HTTPException) as busy_global:
        limiter.connector(limits, 3)
    assert busy_global.value.detail["scope"] == "global"

    a1.release()
    a1.release()  # idempotent: frees one slot, not two
    limiter.connector(limits, 3)
    snapshot = limiter.snapshot(limits)
    assert snapshot["global_in_use"] == 3
    assert snapshot["sources_in_use"] == {"1": 1, "2": 1, "3": 1}
    assert snapshot["rejected"] == {"global": 1, "source": 1, "user": 0}


def test_transfer_slots_are_limited_per_user():
    limiter = SlotLimiter()
    limits = Limits(global_max=10, per_source=10, per_user=2)
    first = limiter.transfer(limits, 7)
    limiter.transfer(limits, 7)
    with pytest.raises(HTTPException) as busy:
        limiter.transfer(limits, 7)
    assert busy.value.detail["scope"] == "user"
    limiter.transfer(limits, 8)  # another user isn't affected
    with first:
        pass  # leaving the block releases it
    limiter.transfer(limits, 7)
    assert limiter.snapshot(limits)["transfers_in_use"] == 3


def test_the_defaults_and_the_settings_they_come_from(session):
    assert current_limits(session) == Limits(global_max=16, per_source=4, per_user=2)
    system_settings.set_int(session, "concurrency_global", 5)
    session.commit()
    assert current_limits(session).global_max == 5


# --- the endpoints ----------------------------------------------------------


@pytest.fixture()
def scratch_dir(tmp_path, monkeypatch):
    path = tmp_path / "scratch"
    monkeypatch.setenv("SCRATCH_DIR", str(path))
    get_settings.cache_clear()
    get_scratch_store.cache_clear()
    yield path
    get_settings.cache_clear()
    get_scratch_store.cache_clear()


@pytest.fixture()
def client(session, scratch_dir):
    app = FastAPI()
    app.include_router(archive_router)
    install_error_handlers(app)
    app.dependency_overrides[get_session] = lambda: session
    role = Role(name="admins", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    app.dependency_overrides[get_current_active_user] = lambda: user
    test_client = TestClient(app)
    test_client.user = user
    return test_client


def _local_source(session, base, *rules: tuple[RuleType, str]) -> Source:
    source = Source(name="logs", protocol=Protocol.local, host="localhost", base_path=str(base))
    session.add(source)
    session.commit()
    session.refresh(source)
    for order, (type_, pattern) in enumerate(rules or [(RuleType.include, "**")]):
        session.add(
            Rule(
                source_id=source.id,
                order=order,
                type=type_,
                pattern=pattern,
                pattern_kind=PatternKind.glob,
            )
        )
    session.commit()
    return source


def _set(session, **values):
    for key, value in values.items():
        system_settings.set_int(session, key, value)
    session.commit()


def test_browse_answers_503_with_retry_after_when_every_slot_is_taken(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log").write_text("x")
    source = _local_source(session, tmp_path / "logs")
    _set(session, concurrency_global=2, concurrency_per_source=2)
    limits = current_limits(session)
    held = [get_limiter().connector(limits, 999), get_limiter().connector(limits, 998)]

    response = client.get(f"/sources/{source.id}/browse")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["detail"]["error_code"] == "busy"
    assert "busy" in response.json()["detail"]["message"]

    held[0].release()
    assert client.get(f"/sources/{source.id}/browse").status_code == 200
    # The request gave its slot back.
    assert get_limiter().snapshot(limits)["global_in_use"] == 1


def test_one_busy_source_does_not_block_another(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    source = _local_source(session, tmp_path / "logs")
    other = _local_source(session, tmp_path / "logs")
    _set(session, concurrency_per_source=1)
    held = get_limiter().connector(current_limits(session), source.id)

    response = client.get(f"/sources/{source.id}/browse")
    assert response.status_code == 503
    assert response.json()["detail"]["scope"] == "source"
    assert client.get(f"/sources/{other.id}/browse").status_code == 200
    held.release()


def test_a_slot_is_released_when_the_listing_fails(session, client, tmp_path):
    source = _local_source(session, tmp_path / "does-not-exist")
    response = client.get(f"/sources/{source.id}/browse")
    assert response.status_code >= 400
    assert get_limiter().snapshot(current_limits(session))["global_in_use"] == 0


def test_downloads_in_progress_are_limited_per_user(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log").write_text("hello")
    source = _local_source(session, tmp_path / "logs")
    limits = current_limits(session)
    held = [get_limiter().transfer(limits, client.user.id) for _ in range(limits.per_user)]

    response = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["detail"]["scope"] == "user"

    held[0].release()
    response = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    assert response.status_code == 200
    assert response.content == b"hello"
    # Released once the file was sent.
    assert get_limiter().snapshot(limits)["transfers_in_use"] == limits.per_user - 1


def test_a_remote_style_open_releases_its_connector_slot(session, client, tmp_path):
    # A .gz goes through scratch (and a connector slot) even on a local source.
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log.gz").write_bytes(gzip.compress(b"line\n"))
    source = _local_source(session, tmp_path / "logs")

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"})
    assert response.status_code == 200
    assert response.content == b"line\n"
    assert get_limiter().snapshot(current_limits(session))["global_in_use"] == 0


def test_a_folder_zip_holds_and_then_releases_both_slots(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("a")
    source = _local_source(session, tmp_path / "logs")
    limits = current_limits(session)
    held = [get_limiter().transfer(limits, client.user.id) for _ in range(limits.per_user)]
    assert client.get(f"/sources/{source.id}/download-zip").status_code == 503
    for slot in held:
        slot.release()

    response = client.get(f"/sources/{source.id}/download-zip")
    assert response.status_code == 200
    snapshot = get_limiter().snapshot(limits)
    assert snapshot["global_in_use"] == 0
    assert snapshot["transfers_in_use"] == 0


# --- streamed folder ZIPs (#345) -------------------------------------------


def test_a_folder_zip_is_streamed_with_the_right_content(session, client, tmp_path, scratch_dir):
    base = tmp_path / "logs"
    (base / "nested").mkdir(parents=True)
    big = os.urandom(3 * 1024 * 1024)  # incompressible: several flushes
    files = {
        "a.log": b"first\n",
        "nested/b.log": b"second\n" * 1000,
        "big.log": big,
        "ünïcode.log": b"names\n",
        "hidden.txt": b"not in the zip",
    }
    for name, content in files.items():
        (base / name).write_bytes(content)
    source = _local_source(session, base, (RuleType.include, "**/*.log"))

    response = client.get(f"/sources/{source.id}/download-zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="logs.zip"'
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == sorted(n for n in files if n.endswith(".log"))
        for name in zf.namelist():
            assert zf.read(name) == files[name]
        # Written to an unseekable stream: sizes follow each member.
        assert all(info.flag_bits & 0x08 for info in zf.infolist())
    # Nothing is left in scratch.
    assert list(scratch_dir.iterdir()) == []


def test_a_folder_zip_of_a_subfolder_keeps_relative_names(session, client, tmp_path):
    base = tmp_path / "logs"
    (base / "nested" / "deeper").mkdir(parents=True)
    (base / "nested" / "deeper" / "c.log").write_text("c")
    source = _local_source(session, base)

    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "nested"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["deeper/c.log"]


def test_an_empty_folder_zip_is_a_valid_zip(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    source = _local_source(session, tmp_path / "logs")
    response = client.get(f"/sources/{source.id}/download-zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == []


# --- 507 when scratch or its disk is full (#345) ---------------------------

_Usage = namedtuple("_Usage", "total used free")


def test_a_fetch_is_refused_with_507_when_scratch_is_full(
    session, client, tmp_path, scratch_dir, monkeypatch
):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log.gz").write_bytes(gzip.compress(b"x"))
    source = _local_source(session, tmp_path / "logs")
    store = get_scratch_store()
    monkeypatch.setattr(store, "max_bytes", 1000)
    # Something nobody can evict (a member being zipped, say).
    (scratch_dir / "perchtail-zip-x").mkdir()
    (scratch_dir / "perchtail-zip-x" / "member-0").write_bytes(b"0" * 2000)

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"})
    assert response.status_code == 507
    assert response.json()["detail"]["error_code"] == "scratch_full"
    assert get_limiter().snapshot(current_limits(session))["global_in_use"] == 0


def test_unreferenced_scratch_entries_are_evicted_to_make_room(tmp_path):
    store = ScratchStore(root=tmp_path / "scratch", max_bytes=1000)
    old = store.acquire("old")
    old.write_bytes(b"0" * 900)
    store.release("old")  # deleted at once: refcount 0
    kept = store.acquire("kept")
    kept.write_bytes(b"0" * 600)
    store.acquire("idle").write_bytes(b"0" * 300)
    store._entries["idle"].refcount = 0  # nobody holds it any more

    store.ensure_room(expected_bytes=200)  # evicts "idle", keeps "kept"
    assert kept.exists()
    assert "idle" not in store._entries
    with pytest.raises(ScratchFullError):
        store.ensure_room(expected_bytes=500)


@pytest.fixture()
def disk_floor(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "scratch_min_free_gb", 1.0)
    monkeypatch.setattr(settings, "scratch_min_free_percent", 5.0)


def test_a_fetch_is_refused_with_507_when_the_disk_is_low(
    session, client, tmp_path, monkeypatch, disk_floor
):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log.gz").write_bytes(gzip.compress(b"x"))
    source = _local_source(session, tmp_path / "logs")
    gib = 1024**3
    # 100 GiB disk with 3 GiB free: under 5% (5 GiB), though over 1 GiB.
    monkeypatch.setattr(
        "app.scratch.shutil.disk_usage", lambda path: _Usage(100 * gib, 97 * gib, 3 * gib)
    )

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"})
    assert response.status_code == 507
    assert response.json()["detail"]["error_code"] == "disk_low"

    monkeypatch.setattr(
        "app.scratch.shutil.disk_usage", lambda path: _Usage(100 * gib, 90 * gib, 10 * gib)
    )
    assert (
        client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"}).status_code == 200
    )


def test_the_free_space_floor_is_the_larger_of_the_two_settings(disk_floor):
    from app.scratch import min_free_bytes

    gib = 1024**3
    assert min_free_bytes(10 * gib) == gib  # 5% would be 0.5 GiB
    assert min_free_bytes(100 * gib) == 5 * gib


def test_a_folder_zip_is_refused_with_507_when_the_disk_is_low(
    session, client, tmp_path, monkeypatch, disk_floor
):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("a")
    source = _local_source(session, tmp_path / "logs")
    monkeypatch.setattr(
        "app.scratch.shutil.disk_usage", lambda path: _Usage(10 * 1024**3, 0, 1024**2)
    )
    response = client.get(f"/sources/{source.id}/download-zip")
    assert response.status_code == 507
    snapshot = get_limiter().snapshot(current_limits(session))
    assert snapshot["global_in_use"] == 0
    assert snapshot["transfers_in_use"] == 0


# --- capped listings (#347) -------------------------------------------------


def test_a_folder_listing_is_capped_with_folders_first(session, client, tmp_path):
    base = tmp_path / "logs"
    base.mkdir()
    for i in range(150):
        (base / f"file-{i:03}.log").write_text("x")
    for name in ("zeta", "alpha"):
        (base / name).mkdir()
        (base / name / "inside.log").write_text("x")
    source = _local_source(session, base)
    _set(session, listing_max_entries=100)

    response = client.get(f"/sources/{source.id}/browse")
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) == 100
    assert response.headers["X-Listing-Truncated"] == "1"
    assert response.headers["X-Listing-Limit"] == "100"
    assert [e["name"] for e in entries[:3]] == ["alpha", "zeta", "file-000.log"]


def test_a_listing_within_the_cap_is_not_marked(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("x")
    source = _local_source(session, tmp_path / "logs")
    response = client.get(f"/sources/{source.id}/browse")
    assert "X-Listing-Truncated" not in response.headers
    assert response.headers["X-Listing-Limit"] == "5000"


def _zip_file(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            if name.endswith("/"):
                zf.writestr(zipfile.ZipInfo(name), "")
            else:
                zf.writestr(name, name)


def test_an_archive_listing_is_capped(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    _zip_file(tmp_path / "logs" / "big.zip", [f"m-{i:03}.log" for i in range(120)])
    source = _local_source(session, tmp_path / "logs")
    _set(session, listing_max_entries=100)

    response = client.get(f"/sources/{source.id}/browse", params={"path": "big.zip"})
    assert len(response.json()) == 100
    assert response.headers["X-Listing-Truncated"] == "1"


def test_an_archive_lists_every_member_at_once_through_the_rules(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    _zip_file(
        tmp_path / "logs" / "x.zip",
        ["a/b/c.log", "a/d.log", "e.log", "empty/", "secret/s.log"],
    )
    source = _local_source(
        session,
        tmp_path / "logs",
        (RuleType.include, "**"),
        (RuleType.exclude, "x.zip/secret/**"),
    )

    response = client.get(f"/sources/{source.id}/browse", params={"path": "x.zip", "all": "true"})
    assert response.status_code == 200
    assert [(e["path"], e["is_dir"]) for e in response.json()] == [
        ("x.zip/a", True),
        ("x.zip/a/b", True),
        ("x.zip/a/b/c.log", False),
        ("x.zip/a/d.log", False),
        ("x.zip/e.log", False),
        ("x.zip/empty", True),
    ]
    # The same as listing it folder by folder.
    top = client.get(f"/sources/{source.id}/browse", params={"path": "x.zip"}).json()
    assert sorted(e["path"] for e in top) == ["x.zip/a", "x.zip/e.log", "x.zip/empty"]


def test_all_members_is_only_for_an_archive(session, client, tmp_path):
    (tmp_path / "logs").mkdir()
    _zip_file(tmp_path / "logs" / "x.zip", ["a/b.log"])
    source = _local_source(session, tmp_path / "logs")
    response = client.get(f"/sources/{source.id}/browse", params={"path": "", "all": "true"})
    assert response.status_code == 400
    response = client.get(f"/sources/{source.id}/browse", params={"path": "x.zip/a", "all": "true"})
    assert response.status_code == 400


def test_an_agent_listing_says_when_the_agent_cut_it_short(monkeypatch):
    from unittest.mock import MagicMock

    from app.collectors import agent as agent_module

    registry = MagicMock()
    registry.send_command_sync.return_value = {
        "entries": [{"name": f"f{i}.log", "is_dir": False, "size": 1} for i in range(5)],
        "truncated": True,
    }
    monkeypatch.setattr(agent_module, "get_agent_registry", lambda: registry)
    source = Source(id=1, name="a", protocol=Protocol.agent, host="agent", base_path="/")
    rule = Rule(source_id=1, order=0, type=RuleType.include, pattern="**")

    entries = agent_module.list_directory(source, [rule], "")
    assert len(entries) == 5
    assert is_truncated(entries)


def test_an_older_agent_sending_too_much_is_cut_on_the_server(monkeypatch):
    from unittest.mock import MagicMock

    from app.collectors import agent as agent_module

    monkeypatch.setattr(agent_module, "AGENT_LIST_MAX_ENTRIES", 3)
    registry = MagicMock()
    registry.send_command_sync.return_value = {
        "entries": [{"name": f"f{i}.log", "is_dir": False, "size": 1} for i in range(5)]
    }
    monkeypatch.setattr(agent_module, "get_agent_registry", lambda: registry)
    source = Source(id=1, name="a", protocol=Protocol.agent, host="agent", base_path="/")
    rule = Rule(source_id=1, order=0, type=RuleType.include, pattern="**")

    entries = agent_module.list_directory(source, [rule], "")
    assert [e.name for e in entries] == ["f0.log", "f1.log", "f2.log"]
    assert is_truncated(entries)
    assert registry.send_command_sync.call_args.kwargs["max_entries"] == 3


def test_a_truncated_source_listing_is_marked_on_browse(session, client, tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    source = _local_source(session, tmp_path / "logs")

    def cut_short(source, rules, relative_path=""):
        listing = DirListing([DirEntry(name="a.log", path="a.log", is_dir=False, size=1)])
        listing.truncated = True
        return listing

    monkeypatch.setattr("app.collectors.local.list_directory", cut_short)
    response = client.get(f"/sources/{source.id}/browse")
    assert [e["name"] for e in response.json()] == ["a.log"]
    assert response.headers["X-Listing-Truncated"] == "1"


# --- recursive walks end (#347) ---------------------------------------------


def test_the_folder_zip_walk_lists_a_looping_folder_once(session, tmp_path):
    from app.collectors.registry import CONNECTORS

    base = tmp_path / "logs"
    (base / "a").mkdir(parents=True)
    (base / "a" / "file.log").write_text("x")
    (base / "b").mkdir()
    os.symlink(base / "a", base / "a" / "back")  # a/back -> a
    os.symlink(base / "a", base / "b" / "to-a")  # b/to-a -> a
    source = _local_source(session, base)
    rules = [Rule(source_id=source.id, order=0, type=RuleType.include, pattern="**")]

    entries = archive_api._zip_entries(CONNECTORS[Protocol.local], source, rules, "")
    # a/back and b/to-a are `a` again: listed once, its file zipped once.
    assert [e.path for e in entries] == ["a/file.log"]


class _EndlessConnector:
    """A source whose every folder holds one more folder, with no identity
    to recognize it by (an agent, say)."""

    def __init__(self):
        self.calls = 0

    def list_directory(self, source, rules, relative_path=""):
        self.calls += 1
        prefix = f"{relative_path}/" if relative_path else ""
        return [
            DirEntry(name="loop", path=f"{prefix}loop", is_dir=True, size=0),
            DirEntry(name="f.log", path=f"{prefix}f.log", is_dir=False, size=1),
        ]


def test_the_folder_zip_walk_stops_at_the_name_index_depth():
    connector = _EndlessConnector()
    source = Source(id=1, name="s", protocol=Protocol.ssh, host="h", base_path="/")
    rules = [Rule(source_id=1, order=0, type=RuleType.include, pattern="**")]

    entries = archive_api._zip_entries(connector, source, rules, "")
    assert connector.calls == MAX_DEPTH
    assert len(entries) == MAX_DEPTH


def test_the_name_index_walk_lists_a_looping_folder_once(session, tmp_path):
    from app.collectors.registry import CONNECTORS
    from app.name_index import _walk

    base = tmp_path / "logs"
    (base / "a").mkdir(parents=True)
    (base / "a" / "file.log").write_text("x")
    os.symlink(base / "a", base / "a" / "back")
    source = _local_source(session, base)
    rules = [Rule(source_id=source.id, order=0, type=RuleType.include, pattern="**")]

    files, result = _walk(CONNECTORS[Protocol.local], source, rules)
    assert set(files) == {"a/file.log"}
    assert result.complete


def test_ssh_listings_carry_each_folders_real_path(monkeypatch):
    import stat as stat_module

    import app.collectors.ssh as ssh_module

    class Attr:
        def __init__(self, name, mode):
            self.filename = name
            self.st_mode = mode
            self.st_size = 0

    class SFTP:
        def normalize(self, path):
            return {"/base/link": "/base"}.get(path, path)

        def listdir_attr(self, path):
            return [Attr("dir", stat_module.S_IFDIR), Attr("link", stat_module.S_IFLNK)]

        def stat(self, path):
            return Attr("x", stat_module.S_IFDIR)

        def close(self):
            pass

        def get_channel(self):
            return type("C", (), {"settimeout": lambda self, s: None})()

    class Client:
        def open_sftp(self):
            return SFTP()

        def close(self):
            pass

    monkeypatch.setattr(ssh_module, "_connect", lambda source: Client())
    source = Source(id=1, name="s", protocol=Protocol.ssh, host="h", base_path="/base")
    rules = [Rule(source_id=1, order=0, type=RuleType.include, pattern="**")]
    entries = {e.name: e.real_key for e in ssh_module.list_directory(source, rules, "")}
    assert entries == {"dir": "/base/dir", "link": "/base"}


# --- RE2 globs (#347) -------------------------------------------------------


def test_rule_globs_run_on_re2_and_keep_their_meaning():
    from app.rules import _compile_glob, glob_matches

    assert "re2" in type(_compile_glob("*.log")._regex).__module__
    assert glob_matches("**/*.log", "var/log/app.log")
    assert glob_matches("**/*.log", "app.log")
    assert not glob_matches("*.log", "var/app.log")
    assert glob_matches("a+b (1)[x]{y}^$|.log", "a+b (1)[x]{y}^$|.log")
    assert not glob_matches("a.log", "aXlog")
    assert glob_matches("APP?.LOG", "app1.log", ignore_case=True)
    assert not glob_matches("APP?.LOG", "app1.log")
    assert glob_matches("ünï/*.log", "ünï/x.log")


def test_a_glob_with_many_stars_matches_in_linear_time():
    from app.rules import glob_matches

    pattern = "*a" * 30 + "b"
    started = time.monotonic()
    assert not glob_matches(pattern, "a" * 5000)
    assert time.monotonic() - started < 1


def test_a_name_with_an_undecodable_byte_still_matches():
    from app.rules import glob_matches

    name = b"bad-\xff.log".decode("utf-8", "surrogateescape")
    assert glob_matches("*.log", name)
    assert glob_matches("bad-?.log", name)


def test_file_name_search_globs_run_on_re2():
    from app.name_index_store import compile_matcher

    matcher = compile_matcher("app-*.log", regex=False)
    assert "re2" in type(matcher.regex).__module__
    assert matcher.matches("var/log/APP-1.log")
    assert not matcher.matches("var/log/app-1.txt")
    assert compile_matcher("logs/**/x?.log", regex=False).matches("logs/a/b/x1.log")
    started = time.monotonic()
    assert not compile_matcher("*a" * 30 + "b", regex=False).matches("a" * 5000)
    assert time.monotonic() - started < 1
