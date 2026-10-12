from pathlib import Path

import pytest
from app.api import archive as archive_module
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.auth.models import Role, User
from app.collectors.base import DirEntry
from app.config import get_settings
from app.db import get_session
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from app.rules import is_safe_relative_path, is_safe_windows_relative_path, is_visible
from app.scratch import get_scratch_store
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _rule(order, rule_type, pattern, kind=PatternKind.glob):
    return Rule(source_id=1, order=order, type=rule_type, pattern=pattern, pattern_kind=kind)


INCLUDE_ALL_EXCLUDE_SECRET = [
    _rule(0, RuleType.include, "**/*"),
    _rule(1, RuleType.exclude, "secret/**"),
]


@pytest.mark.parametrize(
    "path",
    [
        "./secret/a.txt",
        "secret/./a.txt",
        "secret//a.txt",
        "secret/",
        "/secret/a.txt",
        "secret\\a.txt",
        "..\\secret",
        "C:/secret",
        "a.txt:stream",
        "secret/a\x00.txt",
        "secret/a\n.txt",
        "..",
        "logs/../secret/a.txt",
    ],
)
def test_non_canonical_paths_are_rejected(path):
    assert not is_safe_relative_path(path)


@pytest.mark.parametrize("path", ["", "a.log", "logs/app.log", "my logs/app 1.log", "a.b/c.d"])
def test_canonical_paths_are_accepted(path):
    assert is_safe_relative_path(path)


@pytest.mark.parametrize("path", ["secret./a.txt", "secret /a.txt", "SECRE~1/a.txt", "PROGRA~1"])
def test_windows_aliases_are_rejected(path):
    assert is_safe_relative_path(path)
    assert not is_safe_windows_relative_path(path)


@pytest.mark.parametrize("path", ["", "logs/app.log", "report-1.log", "a~b.log"])
def test_ordinary_windows_paths_are_accepted(path):
    assert is_safe_windows_relative_path(path)


def test_case_insensitive_matching_closes_case_bypass():
    rules = [_rule(0, RuleType.include, "**/*.txt"), _rule(1, RuleType.exclude, "**/secret/**")]
    assert is_visible("SECRET/a.txt", rules)
    assert not is_visible("SECRET/a.txt", rules, case_insensitive=True)
    assert is_visible("logs/A.TXT", rules, case_insensitive=True)


def test_case_insensitive_matching_applies_to_regex_rules():
    rules = [_rule(0, RuleType.include, r"\.log$", PatternKind.regex)]
    assert not is_visible("APP.LOG", rules)
    assert is_visible("APP.LOG", rules, case_insensitive=True)


class _FakeConnector:
    def __init__(self):
        self.fetched: list[str] = []

    def list_directory(self, source, rules, relative_path=""):
        return [DirEntry(name="a.txt", path="a.txt", is_dir=False, size=1)]

    def fetch_file(self, source, relative_path, destination: Path):
        self.fetched.append(relative_path)
        destination.write_bytes(b"x")


@pytest.fixture()
def harness(session, tmp_path, monkeypatch):
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    get_settings.cache_clear()
    get_scratch_store.cache_clear()

    connector = _FakeConnector()
    for protocol in (Protocol.ssh, Protocol.smb):
        monkeypatch.setitem(archive_module._CONNECTORS, protocol, connector)

    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    session.commit()

    app = FastAPI()
    app.include_router(archive_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user

    def make_source(protocol):
        source = Source(name=str(protocol), protocol=protocol, host="h", base_path="share")
        session.add(source)
        session.commit()
        for rule in INCLUDE_ALL_EXCLUDE_SECRET:
            session.add(
                Rule(
                    source_id=source.id,
                    order=rule.order,
                    type=rule.type,
                    pattern=rule.pattern,
                    pattern_kind=rule.pattern_kind,
                )
            )
        session.commit()
        return source

    yield TestClient(app), connector, make_source
    get_settings.cache_clear()
    get_scratch_store.cache_clear()


@pytest.mark.parametrize("path", ["./secret/a.txt", "secret//a.txt", "secret\\a.txt"])
def test_open_rejects_non_canonical_spellings_of_an_excluded_file(harness, path):
    client, connector, make_source = harness
    source = make_source(Protocol.ssh)
    assert client.get(f"/sources/{source.id}/open", params={"path": path}).status_code == 400
    assert connector.fetched == []


def test_open_on_windows_source_applies_excludes_case_insensitively(harness):
    client, connector, make_source = harness
    source = make_source(Protocol.smb)
    response = client.get(f"/sources/{source.id}/open", params={"path": "SECRET/a.txt"})
    assert response.status_code == 404
    response = client.get(f"/sources/{source.id}/open", params={"path": "SECRE~1/a.txt"})
    assert response.status_code == 400
    response = client.get(f"/sources/{source.id}/download", params={"path": "secret./a.txt"})
    assert response.status_code == 400
    assert connector.fetched == []


def test_open_still_serves_canonical_visible_files(harness):
    client, connector, make_source = harness
    source = make_source(Protocol.smb)
    response = client.get(f"/sources/{source.id}/open", params={"path": "logs/app.txt"})
    assert response.status_code == 200
    assert connector.fetched == ["logs/app.txt"]
