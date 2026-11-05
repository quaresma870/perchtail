"""Base path validation, location-change cleanup and audit, the Base path
picker (#159), and sources with several roots (#161)."""

import base64
import json

import pytest
from app import source_roots
from app.api import errors as api_errors
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.api.sources import router as sources_router
from app.auth.models import AuditLog, Role, User
from app.collectors import registry
from app.collectors.base import DirEntry
from app.collectors.smb_shares import Share
from app.config import get_settings
from app.db import get_session
from app.models import PatternKind, Protocol, Rule, RuleType, SearchIndexState, Source
from app.timeutils import utcnow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


@pytest.fixture()
def admin(session):
    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def client(session, admin):
    app = FastAPI()
    app.include_router(sources_router)
    app.include_router(archive_router)
    api_errors.install(app)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: admin
    return TestClient(app)


def _create(client, **overrides):
    body = {
        "name": "src",
        "protocol": "ssh",
        "host": "h.example.com",
        "base_path": "/var/log",
        "credential": {"username": "u", "password": "p"},
    }
    body.update(overrides)
    return client.post("/sources", json=body)


def _rule(session, source_id, pattern, type_=RuleType.include, order=0):
    session.add(
        Rule(
            source_id=source_id,
            order=order,
            type=type_,
            pattern=pattern,
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "path, fragment",
    [
        ("C:\\Logs", "not a drive path"),
        ("D:", "not a drive path"),
        ("C$\\Logs", "administrative share"),
        ("ADMIN$", "administrative share"),
        ("\\\\fileserver\\Logs", "server goes in Host"),
        ("", "share name"),
    ],
)
def test_smb_base_path_rejects_paths_that_can_never_work(client, path, fragment):
    response = _create(client, protocol="smb", base_path=path)
    assert response.status_code == 400
    assert fragment in response.json()["detail"]


@pytest.mark.parametrize("path", ["Logs", "Logs\\App", "Logs$", "PTLogs/app"])
def test_smb_base_path_accepts_share_names(client, path):
    assert _create(client, protocol="smb", base_path=path).status_code == 201


@pytest.mark.parametrize("path", ["Logs", "/var/log", "relative\\path"])
def test_winrm_base_path_must_be_absolute(client, path):
    response = _create(client, protocol="winrm", base_path=path)
    assert response.status_code == 400
    assert "absolute Windows path" in response.json()["detail"]


def test_winrm_base_path_on_another_drive_is_accepted(client):
    response = _create(client, protocol="winrm", base_path="E:\\Apps\\Logs\\")
    assert response.status_code == 201
    assert response.json()["base_path"] == "E:\\Apps\\Logs"


def test_unchanged_base_path_is_not_revalidated(session, client):
    """An existing source saved before validation existed keeps saving."""
    source = Source(name="old", protocol=Protocol.smb, host="h", base_path="C$\\Logs")
    session.add(source)
    session.commit()
    response = client.patch(f"/sources/{source.id}", json={"base_path": "C$\\Logs", "name": "x"})
    assert response.status_code == 200
    assert client.patch(f"/sources/{source.id}", json={"base_path": "D$"}).status_code == 400


# --- location change -----------------------------------------------------


def _index_something(session, source_id):
    from app.search_index import _insert_fts_rows

    _insert_fts_rows(session, source_id, "app.log", "secret token here")
    session.add(
        SearchIndexState(source_id=source_id, file_path="app.log", size=1, indexed_at=utcnow())
    )
    session.commit()


@pytest.mark.parametrize(
    "patch", [{"base_path": "/srv/other"}, {"host": "other.example.com"}, {"port": 2222}]
)
def test_moving_a_source_clears_its_index_and_audits_old_and_new(session, client, patch):
    created = _create(client, search_indexing_enabled=True).json()
    _index_something(session, created["id"])

    if "host" in patch or "port" in patch:
        patch = {**patch, "credential": {"username": "u", "password": "p"}}
    assert client.patch(f"/sources/{created['id']}", json=patch).status_code == 200

    assert session.exec(select(SearchIndexState)).all() == []
    event = session.exec(select(AuditLog).where(AuditLog.action == "source.update")).one()
    field = next(k for k in patch if k != "credential")
    change = event.event_metadata["changes"][field]
    assert change["to"] == patch[field]
    assert change["from"] != patch[field]


def test_renaming_keeps_the_index_and_never_audits_credential_values(session, client):
    created = _create(client, search_indexing_enabled=True).json()
    _index_something(session, created["id"])

    client.patch(
        f"/sources/{created['id']}",
        json={"name": "renamed", "credential": {"username": "u", "password": "n3w-s3cret"}},
    )

    assert len(session.exec(select(SearchIndexState)).all()) == 1
    event = session.exec(select(AuditLog).where(AuditLog.action == "source.update")).one()
    assert event.event_metadata["changes"]["credential"] == "replaced"
    assert "n3w-s3cret" not in json.dumps(event.event_metadata)


# --- picker ----------------------------------------------------------------


class _FakeModule:
    """Stands in for a collector module behind the real TranslatingConnector."""

    def __init__(self, tree):
        self.tree = tree
        self.calls = []

    def list_directory(self, source, rules, relative_path=""):
        self.calls.append((source.base_path, source.host, relative_path))
        children = self.tree.get(source.base_path.rstrip("/\\") or "/", None)
        if children is None:
            raise FileNotFoundError(source.base_path)
        return [
            DirEntry(name=n, path=n, is_dir=is_dir, size=0)
            for n, is_dir in children
            if source_roots.ALLOW_ALL is rules or rules
        ]


@pytest.fixture()
def fake_ssh(monkeypatch):
    module = _FakeModule(
        {
            "/": [("var", True), ("etc", True), ("vmlinuz", False)],
            "/var": [("log", True)],
        }
    )
    monkeypatch.setattr(registry.CONNECTORS[Protocol.ssh], "_module", module)
    return module


def test_picker_lists_only_folders_and_audits(session, client, fake_ssh):
    response = client.post(
        "/sources/pick-folder",
        json={
            "protocol": "ssh",
            "host": "h",
            "credential": {"username": "u", "password": "p"},
            "path": "/var",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "protocol": "ssh",
        "path": "/var",
        "parent": "/",
        "kind": "folders",
        "entries": [{"name": "log", "path": "/var/log"}],
        "truncated": False,
    }
    top = client.post(
        "/sources/pick-folder",
        json={"protocol": "ssh", "host": "h", "credential": {"username": "u", "password": "p"}},
    ).json()
    assert [e["name"] for e in top["entries"]] == ["etc", "var"]
    assert top["parent"] is None
    events = session.exec(select(AuditLog).where(AuditLog.action == "source.pick_folder")).all()
    assert [e.event_metadata["path"] for e in events] == ["/var", "/"]


def test_picker_reuses_the_saved_credential_only_for_the_same_host(session, client, fake_ssh):
    created = _create(client).json()

    ok = client.post("/sources/pick-folder", json={"source_id": created["id"], "path": "/"})
    assert ok.status_code == 200

    moved = client.post(
        "/sources/pick-folder",
        json={"source_id": created["id"], "host": "attacker.example.com", "path": "/"},
    )
    assert moved.status_code == 400
    assert "credential again" in moved.json()["detail"]
    assert ("/", "attacker.example.com", "") not in fake_ssh.calls


def test_picker_requires_manage_sources(session, fake_ssh):
    role = Role(name="viewer")
    session.add(role)
    session.commit()
    viewer = User(username="v", role_id=role.id)
    session.add(viewer)
    session.commit()
    app = FastAPI()
    app.include_router(sources_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: viewer
    response = TestClient(app).post(
        "/sources/pick-folder",
        json={"protocol": "ssh", "host": "h", "credential": {"username": "u", "password": "p"}},
    )
    assert response.status_code == 403


def test_picker_maps_source_errors(client, fake_ssh):
    response = client.post(
        "/sources/pick-folder",
        json={
            "protocol": "ssh",
            "host": "h",
            "credential": {"username": "u", "password": "p"},
            "path": "/nope",
        },
    )
    assert response.status_code == 404


def test_picker_winrm_lists_drives_then_folders(client, monkeypatch):
    import app.collectors.winrm as winrm_module

    def run_ps(source, script):
        if "Get-PSDrive" in script:
            payload = json.dumps(["D:\\", "C:\\"])
        else:
            assert "Get-ChildItem -LiteralPath 'D:\\'" in script, script
            payload = json.dumps(
                [
                    {"Name": "Logs", "PSIsContainer": True, "Length": None},
                    {"Name": "pagefile.sys", "PSIsContainer": False, "Length": 1},
                ]
            )
        return base64.b64encode(payload.encode()).decode()

    monkeypatch.setattr(winrm_module, "_run_ps", run_ps)
    creds = {"protocol": "winrm", "host": "w", "credential": {"username": "u", "password": "p"}}

    drives = client.post("/sources/pick-folder", json=creds).json()
    assert drives["kind"] == "drives"
    assert [e["path"] for e in drives["entries"]] == ["C:\\", "D:\\"]

    folders = client.post("/sources/pick-folder", json={**creds, "path": "D:\\"}).json()
    assert folders["kind"] == "folders"
    assert folders["parent"] == ""
    assert folders["entries"] == [{"name": "Logs", "path": "D:\\Logs"}]


def test_picker_smb_top_level_lists_shares(client, monkeypatch):
    from app.collectors import smb_shares

    seen = {}

    def list_shares(host, kwargs):
        seen["host"], seen["port"] = host, kwargs["port"]
        return [Share("AppLogs", ""), Share("PTLogs", "lab")]

    monkeypatch.setattr(smb_shares, "list_shares", list_shares)
    from contextlib import contextmanager

    from app.collectors import smb as smb_collector

    @contextmanager
    def connection(source):
        yield {**smb_collector._connect_kwargs(source), "connection_cache": {}}

    monkeypatch.setattr(smb_collector, "_connection", connection)
    body = client.post(
        "/sources/pick-folder",
        json={
            "protocol": "smb",
            "host": "fs",
            "port": 4455,
            "credential": {"username": "u", "password": "p"},
        },
    ).json()
    assert body["kind"] == "shares"
    assert [e["path"] for e in body["entries"]] == ["AppLogs", "PTLogs"]
    assert seen == {"host": "fs", "port": 4455}


def test_picker_refuses_agent_and_local(client):
    response = client.post("/sources/pick-folder", json={"protocol": "agent", "host": "a"})
    assert response.status_code == 400


# --- several roots -------------------------------------------------------


@pytest.fixture()
def two_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    (allowed / "iis" / "W3SVC1").mkdir(parents=True)
    (allowed / "iis" / "W3SVC1" / "u_ex1.log").write_text("iis line\n")
    (allowed / "app").mkdir()
    (allowed / "app" / "app.log").write_text("app line\n")
    (allowed / "app" / "secret.txt").write_text("nope\n")
    monkeypatch.setattr(get_settings(), "local_source_roots", str(allowed))
    return allowed


def _multi(client, allowed, **overrides):
    return _create(
        client,
        protocol="local",
        host="localhost",
        credential=None,
        roots=[
            {"label": "IIS", "path": str(allowed / "iis")},
            {"label": "App", "path": str(allowed / "app")},
        ],
        **overrides,
    )


def test_multi_root_source_browses_each_root_under_its_label(session, client, two_roots):
    created = _multi(client, two_roots)
    assert created.status_code == 201, created.text
    body = created.json()
    assert [r["label"] for r in body["roots"]] == ["IIS", "App"]
    assert body["base_path"] == str(two_roots / "iis")
    _rule(session, body["id"], "**/*.log")

    top = client.get(f"/sources/{body['id']}/browse", params={"path": ""}).json()
    assert {e["name"] for e in top} == {"IIS", "App"}

    app_entries = client.get(f"/sources/{body['id']}/browse", params={"path": "App"}).json()
    assert [e["path"] for e in app_entries] == ["App/app.log"]

    opened = client.get(f"/sources/{body['id']}/open", params={"path": "IIS/W3SVC1/u_ex1.log"})
    assert opened.status_code == 200
    assert "iis line" in opened.text

    assert (
        client.get(f"/sources/{body['id']}/open", params={"path": "App/secret.txt"}).status_code
        == 404
    )
    assert client.get(f"/sources/{body['id']}/browse", params={"path": "Nope"}).status_code == 404


def test_rules_can_target_one_root(session, client, two_roots):
    body = _multi(client, two_roots).json()
    _rule(session, body["id"], "App/**")

    top = client.get(f"/sources/{body['id']}/browse", params={"path": ""}).json()
    assert [e["name"] for e in top] == ["App"]
    assert client.get(f"/sources/{body['id']}/browse", params={"path": "IIS"}).status_code == 404


def test_check_reports_each_failing_root(session, client, two_roots):
    body = _multi(client, two_roots).json()
    assert client.post(f"/sources/{body['id']}/check").json() == {
        "ok": True,
        "detail": "Connected (2 folders)",
    }
    (two_roots / "app" / "app.log").unlink()
    (two_roots / "app" / "secret.txt").unlink()
    (two_roots / "app").rmdir()

    result = client.post(f"/sources/{body['id']}/check").json()
    assert result["ok"] is False
    assert "folder App" in result["detail"]
    assert "folder IIS" not in result["detail"]


@pytest.mark.parametrize(
    "roots, fragment",
    [
        ([{"label": "a", "path": "/x"}, {"label": "A", "path": "/y"}], "used twice"),
        ([{"label": "a/b", "path": "/x"}, {"label": "c", "path": "/y"}], "must be 1-64"),
        ([{"label": "..", "path": "/x"}, {"label": "c", "path": "/y"}], "must be 1-64"),
    ],
)
def test_root_labels_are_validated(client, roots, fragment):
    response = _create(client, roots=roots)
    assert response.status_code == 400
    assert fragment in response.json()["detail"]


def test_agent_sources_cannot_have_several_roots(client):
    response = _create(
        client,
        protocol="agent",
        credential=None,
        roots=[{"label": "a", "path": "/x"}, {"label": "b", "path": "/y"}],
    )
    assert response.status_code == 400


def test_single_root_list_collapses_to_a_plain_source(session, client, two_roots):
    body = _multi(client, two_roots).json()
    patched = client.patch(
        f"/sources/{body['id']}", json={"roots": [{"label": "x", "path": str(two_roots / "app")}]}
    ).json()
    assert patched["roots"] == []
    assert patched["base_path"] == str(two_roots / "app")
    assert session.get(Source, body["id"]).roots is None


def test_multi_root_local_source_needs_every_root_allowed(client, two_roots, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    response = _create(
        client,
        protocol="local",
        host="localhost",
        credential=None,
        roots=[
            {"label": "a", "path": str(two_roots / "app")},
            {"label": "b", "path": str(outside)},
        ],
    )
    assert response.status_code == 400


# --- configurable folder limit -------------------------------------------


def _set_limit(session, value):
    from app import system_settings

    system_settings.set_int(session, "source_max_roots", value)
    session.commit()


def _roots(base, n):
    return [{"label": f"r{i}", "path": str(base)} for i in range(n)]


def test_folder_limit_is_configurable(session, client, two_roots):
    _set_limit(session, 3)
    too_many = _create(
        client, protocol="local", host="localhost", credential=None, roots=_roots(two_roots, 4)
    )
    assert too_many.status_code == 400
    assert "at most 3 folders" in too_many.json()["detail"]
    ok = _create(
        client, protocol="local", host="localhost", credential=None, roots=_roots(two_roots, 3)
    )
    assert ok.status_code == 201


def test_limit_of_one_turns_several_folders_off(session, client, two_roots):
    _set_limit(session, 1)
    response = _multi(client, two_roots)
    assert response.status_code == 400
    assert "turned off" in response.json()["detail"]


def test_lowering_the_limit_keeps_existing_sources_editable(session, client, two_roots):
    body = _create(
        client, protocol="local", host="localhost", credential=None, roots=_roots(two_roots, 4)
    ).json()
    _set_limit(session, 2)

    same = client.patch(f"/sources/{body['id']}", json={"roots": _roots(two_roots, 4)})
    assert same.status_code == 200
    more = client.patch(f"/sources/{body['id']}", json={"roots": _roots(two_roots, 5)})
    assert more.status_code == 400
    fewer = client.patch(f"/sources/{body['id']}", json={"roots": _roots(two_roots, 3)})
    assert fewer.status_code == 200


def test_stored_limit_is_clamped_to_the_hard_cap(session):
    from app import source_roots
    from app.models import SystemSetting

    session.add(SystemSetting(key="source_max_roots", value="5000"))
    session.commit()
    assert source_roots.max_roots(session) == source_roots.HARD_MAX_ROOTS == 100


@pytest.mark.parametrize("patch", [{"host": "attacker.example.com"}, {"port": 2222}])
def test_repointing_a_source_needs_the_credential_again(session, client, patch):
    """The saved credential is only sent to the host and port it was saved
    for; otherwise editing a source would hand it to any new address."""
    created = _create(client).json()
    refused = client.patch(f"/sources/{created['id']}", json=patch)
    assert refused.status_code == 400
    assert "credential again" in refused.json()["detail"]
    assert session.get(Source, created["id"]).host == "h.example.com"

    with_credential = client.patch(
        f"/sources/{created['id']}",
        json={**patch, "credential": {"username": "u", "password": "p"}},
    )
    assert with_credential.status_code == 200


def test_repointing_a_source_without_a_credential_is_allowed(session, client):
    created = _create(client, protocol="agent", credential=None).json()
    assert client.patch(f"/sources/{created['id']}", json={"host": "other"}).status_code == 200
