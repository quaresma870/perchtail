"""File-name index (#304): matching, the listing pass and the search API."""

import uuid
from datetime import timedelta

import pytest
from app import name_index, system_settings
from app.api.auth import get_current_active_user
from app.api.files import router as files_router
from app.api.sources import router as sources_router
from app.auth.models import Capability, Role, RoleGrant, ScopeType, User
from app.collectors.base import DirEntry, SourcePermissionError
from app.db import get_session
from app.models import FileNameEntry, PatternKind, Protocol, Rule, RuleType, Source
from app.name_index_store import InvalidPattern, compile_matcher, search_entries
from app.timeutils import utcnow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


def _source(session, tmp_path, **overrides) -> Source:
    defaults = dict(
        name=f"src-{uuid.uuid4().hex[:6]}",
        protocol=Protocol.local,
        host="localhost",
        base_path=str(tmp_path),
    )
    defaults.update(overrides)
    source = Source(**defaults)
    session.add(source)
    session.commit()
    session.refresh(source)
    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    return source


def _write(base, rel, text="x"):
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _paths(session, source_id):
    return sorted(
        session.exec(select(FileNameEntry.path).where(FileNameEntry.source_id == source_id)).all()
    )


def _entry(session, source_id, path, size=1):
    session.add(FileNameEntry(source_id=source_id, path=path, size=size, seen_at=utcnow()))
    session.commit()


# --- matching ---------------------------------------------------------------


def test_text_matches_the_name_anywhere_case_insensitively():
    m = compile_matcher("ERR", regex=False)
    assert m.matches("logs/app-error.log")
    assert not m.matches("errors/app.log")  # in a folder name, not the file name


def test_a_pattern_with_a_slash_matches_the_path():
    m = compile_matcher("2026/app", regex=False)
    assert m.matches("logs/2026/app.log")
    assert not m.matches("logs/2025/app.log")


def test_glob_matches_the_whole_name_and_star_stays_in_a_folder():
    m = compile_matcher("app-*.log", regex=False)
    assert m.matches("x/app-1.log")
    assert not m.matches("x/app-1.log.gz")
    deep = compile_matcher("logs/**/*.gz", regex=False)
    assert deep.matches("logs/a/b/c.gz")
    assert not compile_matcher("logs/*.gz", regex=False).matches("logs/a/c.gz")


def test_regex_is_searched_in_the_name_and_an_invalid_one_is_refused():
    m = compile_matcher(r"\.\d+$", regex=True)
    assert m.matches("logs/app.log.3")
    assert not m.matches("logs/app.log")
    with pytest.raises(InvalidPattern):
        compile_matcher("app(", regex=True)


def test_empty_query_matches_everything():
    assert compile_matcher("  ", regex=False) is None


# --- storage and search -----------------------------------------------------


def test_search_uses_the_index_for_long_text_and_like_for_short(session, tmp_path):
    source = _source(session, tmp_path)
    for path in ("a/app.log", "a/app.log.1", "b/db.log", "b/x.txt"):
        _entry(session, source.id, path)
    rows, nxt = search_entries(session, compile_matcher("app.log", False), None, 0, 50)
    assert [r[2] for r in rows] == ["a/app.log", "a/app.log.1"] and nxt is None
    rows, _ = search_entries(session, compile_matcher("db", False), None, 0, 50)
    assert [r[2] for r in rows] == ["b/db.log"]
    rows, _ = search_entries(session, compile_matcher("*.txt", False), None, 0, 50)
    assert [r[2] for r in rows] == ["b/x.txt"]


def test_search_is_limited_to_the_given_sources_and_pages(session, tmp_path):
    one = _source(session, tmp_path)
    two = _source(session, tmp_path)
    for i in range(5):
        _entry(session, one.id, f"app{i}.log")
    _entry(session, two.id, "app-other.log")
    rows, nxt = search_entries(session, compile_matcher("app", False), {one.id}, 0, 3)
    assert len(rows) == 3 and all(r[1] == one.id for r in rows) and nxt == rows[-1][0]
    rest, _ = search_entries(session, compile_matcher("app", False), {one.id}, nxt, 3)
    assert len(rest) == 2
    assert search_entries(session, None, set(), 0, 3) == ([], None)


def test_the_fts_table_follows_deletes_and_updates(session, tmp_path):
    source = _source(session, tmp_path)
    _entry(session, source.id, "keep.log")
    _entry(session, source.id, "gone.log")
    gone = session.exec(select(FileNameEntry).where(FileNameEntry.path == "gone.log")).one()
    session.delete(gone)
    session.commit()
    rows, _ = search_entries(session, compile_matcher(".log", False), None, 0, 10)
    assert [r[2] for r in rows] == ["keep.log"]


# --- the listing pass -------------------------------------------------------


def test_a_pass_adds_updates_and_removes_files(session, tmp_path):
    engine = session.get_bind()
    _write(tmp_path, "app.log")
    _write(tmp_path, "sub/deep/old.log")
    source = _source(session, tmp_path)

    result = name_index.index_source_names(source.id, engine)
    assert result.complete and result.added == 2
    assert _paths(session, source.id) == ["app.log", "sub/deep/old.log"]

    (tmp_path / "sub/deep/old.log").unlink()
    _write(tmp_path, "app.log", "longer content")
    _write(tmp_path, "new.log")
    result = name_index.index_source_names(source.id, engine)
    assert (result.added, result.changed, result.removed) == (1, 1, 1)
    assert _paths(session, source.id) == ["app.log", "new.log"]
    session.expire_all()
    row = session.get(Source, source.id)
    assert row.name_index_files == 2 and row.name_index_message is None and row.name_indexed_at


def test_rules_decide_what_is_indexed(session, tmp_path):
    _write(tmp_path, "app.log")
    _write(tmp_path, "secret/key.pem")
    source = _source(session, tmp_path)
    session.add(
        Rule(
            source_id=source.id,
            order=1,
            type=RuleType.exclude,
            pattern="secret/**",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    name_index.index_source_names(source.id, session.get_bind())
    assert _paths(session, source.id) == ["app.log"]


class _FakeConnector:
    def __init__(self, tree, fail=()):
        self.tree, self.fail = tree, set(fail)

    def list_directory(self, source, rules, path=""):
        if path in self.fail:
            raise SourcePermissionError("denied")
        return self.tree.get(path, [])


def _dir(path):
    return DirEntry(name=path.rsplit("/", 1)[-1], path=path, is_dir=True, size=0)


def _file(path, size=1):
    return DirEntry(name=path.rsplit("/", 1)[-1], path=path, is_dir=False, size=size)


def test_an_unreadable_folder_keeps_what_was_indexed_below_it(session, tmp_path, monkeypatch):
    from app.collectors import registry

    source = _source(session, tmp_path)
    _entry(session, source.id, "locked/old.log")
    tree = {"": [_file("app.log"), _dir("locked")]}
    monkeypatch.setitem(registry.CONNECTORS, Protocol.local, _FakeConnector(tree, fail={"locked"}))
    result = name_index.index_source_names(source.id, session.get_bind())
    assert not result.complete and "couldn't be listed" in result.message
    assert _paths(session, source.id) == ["app.log", "locked/old.log"]


def test_a_source_that_does_not_answer_keeps_its_index(session, tmp_path, monkeypatch):
    from app.collectors import registry

    source = _source(session, tmp_path)
    _entry(session, source.id, "app.log")
    monkeypatch.setitem(registry.CONNECTORS, Protocol.local, _FakeConnector({}, fail={""}))
    result = name_index.index_source_names(source.id, session.get_bind())
    assert not result.complete and result.message
    assert _paths(session, source.id) == ["app.log"]
    session.expire_all()
    assert session.get(Source, source.id).name_index_message


def test_folders_below_the_depth_guard_are_not_listed(session, tmp_path, monkeypatch):
    from app.collectors import registry

    source = _source(session, tmp_path)
    tree, path = {}, ""
    for level in range(name_index.MAX_DEPTH + 2):
        child = f"{path}/d{level}".lstrip("/")
        tree[path] = [_dir(child), _file(f"{child}.log")]
        path = child
    monkeypatch.setitem(registry.CONNECTORS, Protocol.local, _FakeConnector(tree))
    result = name_index.index_source_names(source.id, session.get_bind())
    assert not result.complete and f"below {name_index.MAX_DEPTH} levels" in result.message
    assert result.files == name_index.MAX_DEPTH


def test_sources_opted_out_or_disabled_are_skipped(session, tmp_path):
    off = _source(session, tmp_path, name_index_enabled=False)
    disabled = _source(session, tmp_path, enabled=False)
    assert name_index.index_source_names(off.id, session.get_bind()) is None
    assert name_index.index_source_names(disabled.id, session.get_bind()) is None


def test_due_sources_are_the_stale_ones_and_none_when_off(session, tmp_path):
    fresh = _source(session, tmp_path, name_indexed_at=utcnow())
    stale = _source(session, tmp_path, name_indexed_at=utcnow() - timedelta(hours=2))
    never = _source(session, tmp_path)
    due = name_index.due_source_ids(session)
    assert due.index(never.id) < due.index(stale.id) and fresh.id not in due
    system_settings.set_bool(session, "name_index_enabled", False)
    session.commit()
    assert name_index.due_source_ids(session) == []


# --- API --------------------------------------------------------------------


def _user(session, *, super_admin=False) -> User:
    role = Role(name=f"role-{uuid.uuid4().hex}", is_super_admin=super_admin)
    session.add(role)
    session.commit()
    user = User(username=f"u-{uuid.uuid4().hex[:8]}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _grant(session, user, source, caps=(Capability.view,)):
    session.add(
        RoleGrant(
            role_id=user.role_id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=list(caps),
        )
    )
    session.commit()


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(files_router)
        app.include_router(sources_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def test_search_only_returns_files_of_sources_the_user_can_view(session, tmp_path, client_for):
    mine = _source(session, tmp_path)
    other = _source(session, tmp_path)
    _entry(session, mine.id, "app.log")
    _entry(session, other.id, "app.log")
    user = _user(session)
    _grant(session, user, mine)
    body = client_for(user).get("/files", params={"q": "app"}).json()
    assert [h["source_id"] for h in body["hits"]] == [mine.id]
    assert [s["source_id"] for s in body["sources"]] == [mine.id]
    admin = _user(session, super_admin=True)
    body = client_for(admin).get("/files", params={"q": "app"}).json()
    assert {h["source_id"] for h in body["hits"]} == {mine.id, other.id}


def test_search_refuses_an_invalid_regex_and_says_when_the_index_is_off(
    session, tmp_path, client_for
):
    source = _source(session, tmp_path)
    _entry(session, source.id, "app.log")
    admin = _user(session, super_admin=True)
    client = client_for(admin)
    assert client.get("/files", params={"q": "app(", "regex": "true"}).status_code == 422
    system_settings.set_bool(session, "name_index_enabled", False)
    session.commit()
    body = client.get("/files", params={"q": "app"}).json()
    assert body["hits"] == [] and body["index_enabled"] is False


def test_index_now_needs_run_now_and_the_index_on(session, tmp_path, client_for, monkeypatch):
    started = []
    monkeypatch.setattr(
        "app.api.sources.index_in_background", lambda sid: started.append(sid) or True
    )
    source = _source(session, tmp_path)
    viewer = _user(session)
    _grant(session, viewer, source)
    assert client_for(viewer).post(f"/sources/{source.id}/name-index").status_code == 403
    runner = _user(session)
    _grant(session, runner, source, (Capability.view, Capability.run_now))
    response = client_for(runner).post(f"/sources/{source.id}/name-index")
    assert (
        response.status_code == 202
        and response.json() == {"started": True}
        and started == [source.id]
    )
    source.name_index_enabled = False
    session.add(source)
    session.commit()
    assert client_for(runner).post(f"/sources/{source.id}/name-index").status_code == 409


def test_search_hides_files_a_rule_hides_since_the_last_pass(session, tmp_path, client_for):
    source = _source(session, tmp_path)
    _entry(session, source.id, "app.log")
    _entry(session, source.id, "secret/key.pem")
    session.add(
        Rule(
            source_id=source.id,
            order=1,
            type=RuleType.exclude,
            pattern="secret/**",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    admin = _user(session, super_admin=True)
    body = client_for(admin).get("/files", params={"q": "."}).json()
    assert [h["path"] for h in body["hits"]] == ["app.log"]


def test_regex_search_uses_re2_and_refuses_what_it_cannot_run():
    assert (
        compile_matcher(r"(a+)+$", regex=True).matches("x/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab")
        is False
    )
    with pytest.raises(InvalidPattern):
        compile_matcher(r"(?<=x)log", regex=True)  # lookbehind: not in RE2


def test_short_text_matches_any_letter_case(session, tmp_path):
    source = _source(session, tmp_path)
    _entry(session, source.id, "Évora.log")
    rows, _ = search_entries(session, compile_matcher("é", False), None, 0, 10)
    assert [r[2] for r in rows] == ["Évora.log"]


def test_a_source_is_queued_once(monkeypatch):
    calls = []
    monkeypatch.setattr(name_index, "index_source_names", lambda sid, engine: calls.append(sid))
    blocker = __import__("threading").Event()
    monkeypatch.setattr(name_index._pool, "submit", lambda fn: blocker.wait(0) or None)
    assert name_index._enqueue(4242, None) is True
    assert name_index._enqueue(4242, None) is False
    name_index._pending.discard(4242)
