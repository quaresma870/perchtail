import gzip
import io
import stat as stat_module
import zipfile
from pathlib import Path

import app.collectors.ssh as ssh_module
import pytest
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.auth.models import Capability, Role, RoleGrant, ScopeType, User
from app.config import get_settings
from app.db import get_session
from app.models import Customer, PatternKind, Protocol, Rule, RuleType, Source
from app.scratch import get_scratch_store
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeAttr:
    def __init__(self, filename: str, is_dir: bool, size: int = 0):
        self.filename = filename
        self.st_mode = stat_module.S_IFDIR if is_dir else stat_module.S_IFREG
        self.st_size = size


class FakeSFTPClient:
    def __init__(self):
        self.listing: list[FakeAttr] = []
        self.files: dict[str, bytes] = {}

    def listdir_attr(self, path):
        return self.listing

    def get(self, remote_path, local_path):
        Path(local_path).write_bytes(self.files[remote_path])

    def close(self):
        pass


class FakeSSHClient:
    def __init__(self, sftp: FakeSFTPClient):
        self._sftp = sftp

    def open_sftp(self):
        return self._sftp

    def close(self):
        pass


@pytest.fixture()
def fake_sftp(monkeypatch):
    sftp = FakeSFTPClient()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    return sftp


@pytest.fixture()
def scratch_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    get_settings.cache_clear()
    get_scratch_store.cache_clear()
    store = get_scratch_store()
    yield store
    get_settings.cache_clear()
    get_scratch_store.cache_clear()


@pytest.fixture()
def app_client(session):
    app = FastAPI()
    app.include_router(archive_router)
    app.dependency_overrides[get_session] = lambda: session
    return app, TestClient(app)


def _make_source(session, *, customer=None) -> Source:
    source = Source(
        name="app01",
        customer_id=customer.id if customer else None,
        protocol=Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log/appname",
        credential_ref="encrypted",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _make_local_source(session, base_path) -> Source:
    source = Source(
        name="local-logs",
        protocol=Protocol.local,
        host="localhost",
        base_path=str(base_path),
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _make_user(session, app, *, is_super_admin: bool) -> User:
    role = Role(name=f"role-{is_super_admin}", is_super_admin=is_super_admin)
    session.add(role)
    session.commit()
    session.refresh(role)

    user = User(username=f"user-{role.id}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)

    app.dependency_overrides[get_current_active_user] = lambda: user
    return user


def _add_rule(session, source, order, type_, pattern, kind=PatternKind.glob):
    rule = Rule(source_id=source.id, order=order, type=type_, pattern=pattern, pattern_kind=kind)
    session.add(rule)
    session.commit()


def _grant_view_and_download(session, user, source):
    session.add(
        RoleGrant(
            role_id=user.role_id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=[Capability.view, Capability.download],
        )
    )
    session.commit()


def _zip_bytes(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_browse_lists_directory_filtered_by_rules(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")

    fake_sftp.listing = [
        FakeAttr("nested", is_dir=True),
        FakeAttr("app.log", is_dir=False, size=42),
        FakeAttr("secret.txt", is_dir=False, size=5),
    ]

    response = client.get(f"/sources/{source.id}/browse")
    assert response.status_code == 200
    names = {e["name"] for e in response.json()}
    assert names == {"nested", "app.log"}


def test_browse_rejects_path_traversal(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)

    response = client.get(f"/sources/{source.id}/browse", params={"path": "../../etc"})
    assert response.status_code == 400


def test_browse_denied_without_view_capability(app_client, session, fake_sftp):
    app, client = app_client
    customer = Customer(name="Acme")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = _make_source(session, customer=customer)
    _make_user(session, app, is_super_admin=False)

    response = client.get(f"/sources/{source.id}/browse")
    assert response.status_code == 403


def test_browse_expands_zip_archive_members(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*.zip")

    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_bytes({"app.log": "hello"})

    response = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip"})
    assert response.status_code == 200
    entries = response.json()
    assert entries[0]["name"] == "app.log"
    assert entries[0]["path"] == "logs.zip/app.log"
    assert entries[0]["is_archive"] is False


def test_open_fetches_file_and_returns_scratch_key(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")
    fake_sftp.files["/var/log/appname/app.log"] = b"hello world"

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    assert response.status_code == 200
    assert response.content == b"hello world"
    assert "x-scratch-key" in response.headers


def test_open_denies_file_not_matched_by_rules(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")  # secret.txt won't match
    fake_sftp.files["/var/log/appname/secret.txt"] = b"top secret"

    response = client.get(f"/sources/{source.id}/open", params={"path": "secret.txt"})
    assert response.status_code == 404


def test_open_transparently_decompresses_gz(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log.gz")
    fake_sftp.files["/var/log/appname/app.log.gz"] = gzip.compress(b"hello world")

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"})
    assert response.status_code == 200
    assert response.content == b"hello world"


def test_open_extracts_archive_member(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*.zip")
    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_bytes({"app.log": "hello from zip"})

    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )
    assert response.status_code == 200
    assert response.content == b"hello from zip"


def test_close_releases_the_scratch_entry(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")
    fake_sftp.files["/var/log/appname/app.log"] = b"hello world"

    open_response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    key = open_response.headers["x-scratch-key"]
    assert key in scratch_store._entries

    close_response = client.post(f"/sources/{source.id}/close", json={"path": "app.log"})
    assert close_response.status_code == 204
    assert key not in scratch_store._entries


def test_download_releases_automatically_without_explicit_close(
    app_client, session, fake_sftp, scratch_store
):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "*.log")
    fake_sftp.files["/var/log/appname/app.log"] = b"hello world"

    response = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    assert response.status_code == 200
    assert response.content == b"hello world"
    assert len(scratch_store._entries) == 0


def test_download_requires_download_capability_not_just_view(app_client, session, fake_sftp):
    app, client = app_client
    customer = Customer(name="Acme")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = _make_source(session, customer=customer)
    user = _make_user(session, app, is_super_admin=False)
    session.add(
        RoleGrant(
            role_id=user.role_id,
            scope_type=ScopeType.customer,
            scope_id=customer.id,
            capabilities=[Capability.view],  # view only, no download
        )
    )
    session.commit()
    _add_rule(session, source, 0, RuleType.include, "*.log")

    response = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    assert response.status_code == 403


def test_open_local_plain_file_skips_scratch_entirely(app_client, session, scratch_store, tmp_path):
    app, client = app_client
    (tmp_path / "app.log").write_text("hello world")
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    assert response.status_code == 200
    assert response.content == b"hello world"
    assert "x-scratch-key" not in response.headers
    assert len(scratch_store._entries) == 0


def test_open_local_gz_file_still_uses_scratch(app_client, session, scratch_store, tmp_path):
    app, client = app_client
    (tmp_path / "app.log.gz").write_bytes(gzip.compress(b"hello world"))
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log.gz")

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"})
    assert response.status_code == 200
    assert response.content == b"hello world"
    assert "x-scratch-key" in response.headers
