import gzip
import io
import stat as stat_module
import tarfile
import zipfile
from pathlib import Path

import app.collectors.ssh as ssh_module
import pytest
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.auth.models import AuditLog, Capability, Role, RoleGrant, ScopeType, User
from app.config import get_settings
from app.db import get_session
from app.models import Customer, PatternKind, Protocol, Rule, RuleType, Source
from app.scratch import get_scratch_store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


class FakeAttr:
    def __init__(self, filename: str, is_dir: bool, size: int = 0):
        self.filename = filename
        self.st_mode = stat_module.S_IFDIR if is_dir else stat_module.S_IFREG
        self.st_size = size


class FakeSFTPClient:
    def __init__(self):
        self.listing: list[FakeAttr] = []
        self.files: dict[str, bytes] = {}

    def normalize(self, path):
        return path

    def listdir_attr(self, path):
        return self.listing

    def get(self, remote_path, local_path):
        Path(local_path).write_bytes(self.files[remote_path])

    def stat(self, remote_path):
        return FakeAttr(remote_path.rsplit("/", 1)[-1], False, len(self.files[remote_path]))

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
    _add_rule(session, source, 0, RuleType.include, "**/*.log")

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
    _add_rule(session, source, 0, RuleType.include, "**/*")

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


def test_open_gz_over_the_decompression_limit_is_a_413(
    app_client, session, fake_sftp, scratch_store, monkeypatch
):
    monkeypatch.setattr(get_settings(), "max_decompressed_mb", 0.001)  # ~1 KB
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log.gz")
    fake_sftp.files["/var/log/appname/app.log.gz"] = gzip.compress(b"A" * 100_000)

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"})
    assert response.status_code == 413
    assert list(scratch_store.root.iterdir()) == []


def test_open_extracts_archive_member(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_bytes({"app.log": "hello from zip"})

    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )
    assert response.status_code == 200
    assert response.content == b"hello from zip"


def test_browse_archive_filters_members_not_matched_by_rules(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    # Visible: the archive itself, and *.log members inside it. Not visible:
    # secret.txt inside it — a rule scoped to the archive shouldn't leak
    # everything packed inside it.
    _add_rule(session, source, 0, RuleType.include, "**/*.zip")
    _add_rule(session, source, 1, RuleType.include, "**/*.log")
    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_bytes(
        {"app.log": "hello", "secret.txt": "nope"}
    )

    response = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip"})
    assert response.status_code == 200
    names = {e["name"] for e in response.json()}
    assert names == {"app.log"}


def test_open_archive_member_denied_when_rules_dont_cover_it(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    # Only the archive itself is rule-visible — its contents are not, so a
    # direct /open call for a member must be denied even though it's a real
    # entry inside a real, reachable archive (bypassing the browse listing
    # filter isn't a way around the rule engine).
    _add_rule(session, source, 0, RuleType.include, "**/*.zip")
    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_bytes(
        {"app.log": "hello", "secret.txt": "nope"}
    )

    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "secret.txt"}
    )
    assert response.status_code == 404


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


def test_open_with_an_empty_path_is_a_400_not_the_base_directory(
    app_client, session, scratch_store, tmp_path
):
    app, client = app_client
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")

    assert client.get(f"/sources/{source.id}/open", params={"path": ""}).status_code == 400
    assert client.get(f"/sources/{source.id}/download", params={"path": ""}).status_code == 400


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


def test_download_zip_bundles_only_rule_visible_files(app_client, session, tmp_path):
    app, client = app_client
    (tmp_path / "app.log").write_text("hello")
    (tmp_path / "secret.txt").write_text("nope")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "deep.log").write_text("deep")

    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*.log")

    response = client.get(f"/sources/{source.id}/download-zip")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = set(zf.namelist())
        assert names == {"app.log", "nested/deep.log"}
        assert zf.read("app.log") == b"hello"
        assert zf.read("nested/deep.log") == b"deep"


def test_download_zip_denied_without_download_capability(app_client, session, tmp_path):
    app, client = app_client
    (tmp_path / "app.log").write_text("hello")
    customer = Customer(name="Acme")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = _make_local_source(session, tmp_path)
    source.customer_id = customer.id
    session.add(source)
    session.commit()

    user = _make_user(session, app, is_super_admin=False)
    session.add(
        RoleGrant(
            role_id=user.role_id,
            scope_type=ScopeType.customer,
            scope_id=customer.id,
            capabilities=[Capability.view],
        )
    )
    session.commit()

    response = client.get(f"/sources/{source.id}/download-zip")
    assert response.status_code == 403


def test_download_zip_of_subfolder_uses_relative_arcnames(app_client, session, tmp_path):
    app, client = app_client
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "deep.log").write_text("deep")
    (tmp_path / "app.log").write_text("top-level, excluded from this request")

    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*.log")

    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "nested"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["deep.log"]


def test_browse_root_logs_source_open_audit_event(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    fake_sftp.listing = [FakeAttr("app.log", is_dir=False, size=1)]

    response = client.get(f"/sources/{source.id}/browse")
    assert response.status_code == 200

    events = session.exec(select(AuditLog).where(AuditLog.action == "source.open")).all()
    assert len(events) == 1
    assert events[0].user_id == user.id
    assert events[0].target_type == "source"
    assert events[0].target_id == source.id


def test_browse_subdirectory_does_not_log_audit_event(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.listing = [FakeAttr("nested.log", is_dir=False, size=1)]

    response = client.get(f"/sources/{source.id}/browse", params={"path": "nested"})
    assert response.status_code == 200

    events = session.exec(select(AuditLog).where(AuditLog.action == "source.open")).all()
    assert events == []


def test_browse_root_twice_logs_two_events_not_deduped(app_client, session, fake_sftp):
    # Dedup for the "recent connections" list happens on read (GET
    # /sources/recent), not on write -- every root browse is a real event.
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    fake_sftp.listing = []

    client.get(f"/sources/{source.id}/browse")
    client.get(f"/sources/{source.id}/browse")

    events = session.exec(select(AuditLog).where(AuditLog.action == "source.open")).all()
    assert len(events) == 2


def test_download_logs_file_download_audit_event(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "*.log")
    fake_sftp.files["/var/log/appname/app.log"] = b"hello world"

    response = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    assert response.status_code == 200

    events = session.exec(select(AuditLog).where(AuditLog.action == "file.download")).all()
    assert len(events) == 1
    assert events[0].user_id == user.id
    assert events[0].target_id == source.id
    assert events[0].event_metadata["path"] == "app.log"
    assert events[0].event_metadata["target_name"] == source.name  # #239


def test_open_logs_a_file_open_audit_event(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")
    fake_sftp.files["/var/log/appname/app.log"] = b"hello world"

    assert client.get(f"/sources/{source.id}/open", params={"path": "app.log"}).status_code == 200

    events = session.exec(select(AuditLog).where(AuditLog.action == "file.open")).all()
    assert len(events) == 1
    assert events[0].user_id == user.id
    assert events[0].target_id == source.id
    assert events[0].event_metadata["path"] == "app.log"
    assert events[0].event_metadata["target_name"] == source.name  # #239


def test_download_zip_logs_a_folder_download_audit_event(app_client, session, tmp_path):
    app, client = app_client
    (tmp_path / "app.log").write_text("hello")
    (tmp_path / "other.log").write_text("hi")
    source = _make_local_source(session, tmp_path)
    user = _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")

    assert client.get(f"/sources/{source.id}/download-zip").status_code == 200

    events = session.exec(select(AuditLog).where(AuditLog.action == "folder.download")).all()
    assert len(events) == 1
    assert events[0].user_id == user.id
    assert events[0].event_metadata["file_count"] == 2
    assert events[0].event_metadata["target_name"] == source.name  # #239


def test_open_over_max_fetch_mb_is_a_413(
    app_client, session, fake_sftp, scratch_store, monkeypatch
):
    monkeypatch.setattr(get_settings(), "max_fetch_mb", 0.001)  # ~1 KB
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")
    fake_sftp.files["/var/log/appname/big.log"] = b"x" * 5000

    response = client.get(f"/sources/{source.id}/open", params={"path": "big.log"})
    assert response.status_code == 413
    assert list(scratch_store.root.iterdir()) == []


def test_folder_zip_over_max_fetch_mb_in_total_is_a_413(app_client, session, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_fetch_mb", 0.001)  # ~1 KB in total
    app, client = app_client
    for i in range(3):
        (tmp_path / f"part{i}.log").write_text("x" * 600)
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")

    assert client.get(f"/sources/{source.id}/download-zip").status_code == 413


def test_folder_zip_over_the_limit_fails_before_fetching_anything(
    app_client, session, tmp_path, monkeypatch
):
    """#191: the listed sizes are summed first, so an oversized folder gets
    its 413 (with the folder's size) without downloading up to the limit."""
    import app.collectors.local as local_connector

    monkeypatch.setattr(get_settings(), "max_fetch_mb", 0.001)  # ~1 KB in total
    fetched = []
    real_fetch = local_connector.fetch_file
    monkeypatch.setattr(
        local_connector,
        "fetch_file",
        lambda *a, **kw: (fetched.append(a[1]), real_fetch(*a, **kw))[1],
    )
    app, client = app_client
    for i in range(3):
        (tmp_path / f"part{i}.log").write_text("x" * 600)
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "*.log")

    response = client.get(f"/sources/{source.id}/download-zip")

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["size_bytes"] == 1800
    assert detail["message"].startswith("This folder is")
    assert fetched == []


def test_symlink_escaping_base_path_is_a_404_on_browse_and_open(
    app_client, session, monkeypatch, scratch_store
):
    """Lab finding F-10: `escape -> /etc` inside an SSH base path let a
    view-only user read /etc/passwd. Both browse and open must answer 404,
    the same as a rule-hidden path."""
    from app.collectors.base import PathOutsideBaseError
    from app.main import path_outside_base

    app, client = app_client
    app.add_exception_handler(PathOutsideBaseError, path_outside_base)
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")

    class EscapingSFTP(FakeSFTPClient):
        def normalize(self, path):
            return "/etc" if path.startswith("/var/log/appname/escape") else path

    sftp = EscapingSFTP()
    sftp.files["/etc/passwd"] = b"root:x:0:0"
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    browse = client.get(f"/sources/{source.id}/browse", params={"path": "escape"})
    opened = client.get(f"/sources/{source.id}/open", params={"path": "escape/passwd"})

    assert browse.status_code == 404
    assert opened.status_code == 404
    assert b"root:" not in opened.content


# --- every protocol is served by browse/open (regression: #165) ------------


def test_every_protocol_has_a_connector():
    from app.collectors.registry import CONNECTORS

    assert set(CONNECTORS) == set(Protocol)


def test_agent_source_can_be_browsed_and_opened(app_client, session, monkeypatch, scratch_store):
    """v0.4.0 answered 501 "protocol agent not yet supported" on every
    browse/open of an agent source while /check passed: archive.py kept its
    own connector map without the agent."""
    import base64

    from app.agent_registry import get_agent_registry

    app, client = app_client
    _make_user(session, app, is_super_admin=True)
    source = Source(
        name="agent01",
        protocol=Protocol.agent,
        host="agent01",
        base_path="/var/log/app",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    _add_rule(session, source, 0, RuleType.include, "**/*")

    def fake_send(source_id, command_type, timeout=30.0, **fields):
        assert source_id == source.id
        if command_type == "list":
            return {"entries": [{"name": "app.log", "is_dir": False, "size": 5}]}
        assert command_type == "fetch" and fields["path"] == "app.log"
        return {"content_b64": base64.b64encode(b"hello").decode()}

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", fake_send)

    browse = client.get(f"/sources/{source.id}/browse")
    opened = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})

    assert browse.status_code == 200, browse.text
    assert [e["name"] for e in browse.json()] == ["app.log"]
    assert opened.status_code == 200, opened.text
    assert opened.content == b"hello"


# --- folders hidden by the rules are not browsable (#164) -------------------


def test_excluded_folder_is_neither_listed_nor_browsable(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "secret/**")
    fake_sftp.listing = [FakeAttr("app", is_dir=True), FakeAttr("secret", is_dir=True)]

    root = client.get(f"/sources/{source.id}/browse")
    inside = client.get(f"/sources/{source.id}/browse", params={"path": "secret"})
    deeper = client.get(f"/sources/{source.id}/browse", params={"path": "secret/sub"})

    assert {e["name"] for e in root.json()} == {"app"}
    assert inside.status_code == 404
    assert deeper.status_code == 404


def test_hidden_folder_cannot_be_downloaded_as_zip(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "secret/**")

    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "secret"})

    assert response.status_code == 404


# --- archives browse as a folder tree (#175) --------------------------------


def test_archive_members_are_listed_as_a_tree(app_client, session, fake_sftp):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_bytes(
        {"raiz.log": "r", "sub/pasta/membro.log": "m", "sub/outro.log": "o"}
    )

    top = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip"})
    sub = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip/sub"})
    deep = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip/sub/pasta"})

    assert top.status_code == 200, top.text
    assert [(e["name"], e["is_dir"]) for e in top.json()] == [("sub", True), ("raiz.log", False)]
    assert [(e["name"], e["is_dir"]) for e in sub.json()] == [
        ("pasta", True),
        ("outro.log", False),
    ]
    assert [e["path"] for e in deep.json()] == ["logs.zip/sub/pasta/membro.log"]


def test_member_with_a_traversal_name_is_shown_normalized_and_opens(
    app_client, session, fake_sftp, scratch_store
):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files["/var/log/appname/bad.zip"] = _zip_bytes({"../../evil.txt": "e"})

    listing = client.get(f"/sources/{source.id}/browse", params={"path": "bad.zip"})
    opened = client.get(
        f"/sources/{source.id}/open", params={"path": "bad.zip", "member": "evil.txt"}
    )

    assert [e["path"] for e in listing.json()] == ["bad.zip/evil.txt"]
    assert opened.status_code == 200
    assert opened.content == b"e"


def test_open_is_served_inline_as_text(app_client, session, fake_sftp, scratch_store):
    """#174: /open is what the viewer reads, not a download endpoint."""
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files["/var/log/appname/app.log"] = b"hello"

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})

    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["content-type"].startswith("text/plain")


def test_413_reports_file_size_and_limit(
    app_client, session, fake_sftp, scratch_store, monkeypatch
):
    """#160: the viewer needs to say by how much a file is over the limit."""
    monkeypatch.setattr("app.collectors.base.max_fetch_bytes", lambda: 8)
    monkeypatch.setattr("app.api.archive.max_fetch_bytes", lambda: 8)
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files["/var/log/appname/big.log"] = b"x" * 100

    response = client.get(f"/sources/{source.id}/open", params={"path": "big.log"})

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["error_code"] == "fetch_too_large"
    assert detail["size_bytes"] == 100
    assert detail["limit_bytes"] == 8


def test_source_open_names_the_source_and_its_customer(app_client, session, fake_sftp):
    """#239: opening a source recorded only `source` and an id."""
    app, client = app_client
    customer = Customer(name="Acme")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = _make_source(session, customer=customer)
    _make_user(session, app, is_super_admin=True)
    fake_sftp.listing = [FakeAttr("app.log", is_dir=False, size=1)]

    assert client.get(f"/sources/{source.id}/browse").status_code == 200

    event = session.exec(select(AuditLog).where(AuditLog.action == "source.open")).one()
    assert event.event_metadata == {"target_name": source.name, "target_context": "Acme"}


# --- whole archives with members hidden by the rules ------------------------


def _tar_gz_bytes(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in members.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_ARCHIVE_BUILDERS = {"logs.zip": _zip_bytes, "logs.tar.gz": _tar_gz_bytes}


@pytest.mark.parametrize("archive", sorted(_ARCHIVE_BUILDERS))
def test_whole_archive_with_an_excluded_member_is_refused(
    app_client, session, fake_sftp, scratch_store, archive
):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, f"{archive}/secret.txt")
    fake_sftp.files[f"/var/log/appname/{archive}"] = _ARCHIVE_BUILDERS[archive](
        {"app.log": "hello", "secret.txt": "nope"}
    )
    url = f"/sources/{source.id}"

    whole = client.get(f"{url}/download", params={"path": archive})
    opened = client.get(f"{url}/open", params={"path": archive, "member": "app.log"})
    downloaded = client.get(f"{url}/download", params={"path": archive, "member": "app.log"})
    hidden = client.get(f"{url}/download", params={"path": archive, "member": "secret.txt"})

    assert whole.status_code == 403
    assert whole.json()["detail"] == {
        "message": "This archive can't be downloaded whole: open or download its files one by one.",
        "error_code": "archive_not_downloadable_whole",
    }
    assert b"nope" not in whole.content
    assert b"hidden" not in whole.content and b"rule" not in whole.content
    denied = session.exec(select(AuditLog).where(AuditLog.action == "file.download_denied")).one()
    assert denied.user_id == user.id
    assert denied.target_type == "source" and denied.target_id == source.id
    assert denied.event_metadata["path"] == archive
    assert denied.event_metadata["reason"] == "archive_hidden_members"
    downloads = session.exec(select(AuditLog).where(AuditLog.action == "file.download")).all()
    assert [entry.event_metadata["member"] for entry in downloads] == ["app.log"]
    assert opened.status_code == 200 and opened.content == b"hello"
    assert downloaded.status_code == 200 and downloaded.content == b"hello"
    assert hidden.status_code == 404
    assert len(scratch_store._entries) == 1  # only the /open above, still held


def test_whole_archive_is_refused_on_open_too(app_client, session, fake_sftp, scratch_store):
    """/open without a member hands over the archive's bytes just like
    /download does."""
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/secret.txt")
    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_bytes({"a.log": "a", "secret.txt": "x"})

    response = client.get(f"/sources/{source.id}/open", params={"path": "logs.zip"})

    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
    assert len(scratch_store._entries) == 0
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.open_denied")).one()
    assert event.target_type == "source" and event.target_id == source.id
    assert event.event_metadata["path"] == "logs.zip"
    assert event.event_metadata["reason"] == "archive_hidden_members"
    assert session.exec(select(AuditLog).where(AuditLog.action == "file.open")).first() is None


@pytest.mark.parametrize("archive", sorted(_ARCHIVE_BUILDERS))
def test_archive_shown_by_a_rule_on_the_archive_alone_downloads_whole(
    app_client, session, fake_sftp, scratch_store, archive
):
    """Regression: no rule matches the members, so nothing is hidden on
    purpose and the archive downloads byte for byte, as before."""
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "**/*.zip")
    _add_rule(session, source, 1, RuleType.include, "**/*.tar.gz")
    original = _ARCHIVE_BUILDERS[archive]({"app.log": "hello", "secret.txt": "nope"})
    fake_sftp.files[f"/var/log/appname/{archive}"] = original

    response = client.get(f"/sources/{source.id}/download", params={"path": archive})

    assert response.status_code == 200
    assert response.content == original
    assert len(scratch_store._entries) == 0


def test_generic_exclude_catches_a_member(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/*.tmp")
    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_bytes(
        {"app.log": "hello", "sub/cache.tmp": "nope"}
    )

    response = client.get(f"/sources/{source.id}/download", params={"path": "logs.zip"})

    assert response.status_code == 403


def test_whole_archive_is_read_from_the_source_once(app_client, session, fake_sftp, scratch_store):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/*.tmp")
    original = _zip_bytes({"app.log": "hello"})
    fake_sftp.files["/var/log/appname/logs.zip"] = original
    reads = []
    real_get = fake_sftp.get
    fake_sftp.get = lambda remote, local: (reads.append(remote), real_get(remote, local))[1]

    response = client.get(f"/sources/{source.id}/download", params={"path": "logs.zip"})

    assert response.status_code == 200
    assert response.content == original
    assert reads == ["/var/log/appname/logs.zip"]


def test_case_insensitive_source_matches_members_ignoring_case(
    app_client, session, monkeypatch, scratch_store
):
    """A Windows agent source (case-insensitive rules, fetched through
    local_copy): `SECRET.log` inside the archive is caught by an exclude
    written as `secret.log`."""
    import base64

    from app.agent_registry import get_agent_registry

    app, client = app_client
    user = _make_user(session, app, is_super_admin=True)
    source = Source(
        name="agent01",
        protocol=Protocol.agent,
        host="agent01",
        base_path="C:/logs",
        agent_os="windows",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/secret.log")
    archive = _zip_bytes({"app.log": "hello", "SECRET.log": "nope"})

    def fake_send(source_id, command_type, timeout=30.0, **fields):
        assert command_type == "fetch" and fields["path"] == "logs.zip"
        return {"content_b64": base64.b64encode(archive).decode()}

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", fake_send)

    response = client.get(f"/sources/{source.id}/download", params={"path": "logs.zip"})

    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "archive_not_downloadable_whole"


@pytest.mark.parametrize(("with_exclude", "expected"), [(True, 403), (False, 200)])
def test_corrupt_archive_is_refused_only_when_the_source_has_an_exclude(
    app_client, session, fake_sftp, scratch_store, with_exclude, expected
):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    if with_exclude:
        _add_rule(session, source, 1, RuleType.exclude, "**/*.tmp")
    fake_sftp.files["/var/log/appname/broken.zip"] = b"not really a zip"

    response = client.get(f"/sources/{source.id}/download", params={"path": "broken.zip"})

    assert response.status_code == expected
    if expected == 200:
        assert response.content == b"not really a zip"


def test_local_source_whole_archive_with_an_excluded_member_is_refused(
    app_client, session, scratch_store, tmp_path
):
    app, client = app_client
    (tmp_path / "logs.zip").write_bytes(_zip_bytes({"app.log": "a", "secret.txt": "x"}))
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/secret.txt")

    response = client.get(f"/sources/{source.id}/download", params={"path": "logs.zip"})

    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.download_denied")).one()
    assert event.event_metadata["reason"] == "archive_hidden_members"


def test_folder_zip_leaves_out_archives_with_hidden_members(app_client, session, tmp_path):
    app, client = app_client
    (tmp_path / "app.log").write_text("hello")
    clean = _zip_bytes({"a.log": "a"})
    (tmp_path / "clean.zip").write_bytes(clean)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "hiding.tar.gz").write_bytes(_tar_gz_bytes({"b.log": "b", "secret.txt": "x"}))
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/secret.txt")

    response = client.get(f"/sources/{source.id}/download-zip")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert set(zf.namelist()) == {"app.log", "clean.zip", "SKIPPED.txt"}
        assert zf.read("app.log") == b"hello"
        assert zf.read("clean.zip") == clean
        assert zf.read("SKIPPED.txt") == (
            b"nested/hiding.tar.gz: can't be included whole; open or download its files one by "
            b"one\n"
        )
    event = session.exec(select(AuditLog).where(AuditLog.action == "folder.download")).one()
    assert event.event_metadata["file_count"] == 2
    assert event.event_metadata["skipped_archives"] == ["nested/hiding.tar.gz"]


@pytest.mark.parametrize("with_exclude", [True, False])
def test_archive_inside_an_archive_counts_as_hidden_when_the_source_has_an_exclude(
    app_client, session, scratch_store, tmp_path, with_exclude
):
    """The rules are never checked against an inner archive's own members,
    so with an exclude rule on the source the outer archive can't be
    downloaded whole, the inner one can't be taken whole through `member=`,
    and a folder's ZIP leaves the outer one out. With no exclude rule, all
    three work as before."""
    app, client = app_client
    inner = _zip_bytes({"app.log": "a", "secret.key": "x"})
    outer = _zip_bytes({"inner.zip": inner, "readme.log": "r"})
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "outer.zip").write_bytes(outer)
    (logs / "app.log").write_text("hello")
    source = _make_local_source(session, tmp_path)
    user = _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    if with_exclude:
        _add_rule(session, source, 1, RuleType.exclude, "**/*.key")
    url = f"/sources/{source.id}"
    member = {"path": "logs/outer.zip", "member": "inner.zip"}

    whole = client.get(f"{url}/download", params={"path": "logs/outer.zip"})
    member_download = client.get(f"{url}/download", params=member)
    member_open = client.get(f"{url}/open", params=member)
    readme = client.get(f"{url}/download", params={**member, "member": "readme.log"})
    folder = client.get(f"{url}/download-zip", params={"path": "logs"})

    assert readme.status_code == 200 and readme.content == b"r"
    assert folder.status_code == 200
    with zipfile.ZipFile(io.BytesIO(folder.content)) as zf:
        names = set(zf.namelist())
        if with_exclude:
            assert names == {"app.log", "SKIPPED.txt"}
        else:
            assert names == {"app.log", "outer.zip"}
            assert zf.read("outer.zip") == outer
    if not with_exclude:
        assert whole.status_code == 200 and whole.content == outer
        assert member_download.status_code == 200 and member_download.content == inner
        assert member_open.status_code == 200 and member_open.content == inner
        denied_actions = ["file.download_denied", "file.open_denied"]
        denied = session.exec(select(AuditLog).where(AuditLog.action.in_(denied_actions))).all()
        assert denied == []
        return
    for response in (whole, member_download, member_open):
        assert response.status_code == 403
        assert response.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
        assert b"secret" not in response.content
    denied_downloads = session.exec(
        select(AuditLog).where(AuditLog.action == "file.download_denied")
    ).all()
    assert [entry.event_metadata["path"] for entry in denied_downloads] == [
        "logs/outer.zip",
        "logs/outer.zip/inner.zip",
    ]
    opened = session.exec(select(AuditLog).where(AuditLog.action == "file.open_denied")).one()
    assert opened.user_id == user.id and opened.target_id == source.id
    assert opened.event_metadata["path"] == "logs/outer.zip/inner.zip"
    assert opened.event_metadata["reason"] == "archive_hidden_members"
    # A refused member leaves nothing held in scratch.
    assert len(scratch_store._entries) == 0


def test_upper_case_inner_archive_counts_as_hidden_with_an_exclude(
    app_client, session, scratch_store, tmp_path
):
    """`INNER.ZIP` is an archive just like `inner.zip`."""
    app, client = app_client
    inner = _zip_bytes({"app.log": "a", "secret.key": "x"})
    (tmp_path / "outer.zip").write_bytes(_zip_bytes({"INNER.ZIP": inner, "readme.log": "r"}))
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/*.key")
    url = f"/sources/{source.id}"

    whole = client.get(f"{url}/download", params={"path": "outer.zip"})
    member = client.get(f"{url}/download", params={"path": "outer.zip", "member": "INNER.ZIP"})

    for response in (whole, member):
        assert response.status_code == 403
        assert response.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
    assert len(scratch_store._entries) == 0


def test_upper_case_top_level_archive_is_an_archive(app_client, session, fake_sftp, scratch_store):
    """`OLD.ZIP` is listed as an archive in the tree, and a whole download is
    refused when one of its members is excluded."""
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/secret.txt")
    fake_sftp.listing = [FakeAttr("OLD.ZIP", is_dir=False, size=10)]
    fake_sftp.files["/var/log/appname/OLD.ZIP"] = _zip_bytes(
        {"app.log": "hello", "secret.txt": "nope"}
    )
    url = f"/sources/{source.id}"

    listed = client.get(f"{url}/browse")
    members = client.get(f"{url}/browse", params={"path": "OLD.ZIP"})
    whole = client.get(f"{url}/download", params={"path": "OLD.ZIP"})
    opened = client.get(f"{url}/open", params={"path": "OLD.ZIP", "member": "app.log"})

    assert listed.status_code == 200
    assert [(e["name"], e["is_archive"]) for e in listed.json()] == [("OLD.ZIP", True)]
    assert {e["name"] for e in members.json()} == {"app.log"}
    assert whole.status_code == 403
    assert whole.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
    assert opened.status_code == 200 and opened.content == b"hello"


def test_upper_case_gz_extension_is_decompressed_and_served_without_it(
    app_client, session, fake_sftp, scratch_store
):
    app, client = app_client
    source = _make_source(session)
    user = _make_user(session, app, is_super_admin=True)
    _grant_view_and_download(session, user, source)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files["/var/log/appname/app.log.GZ"] = gzip.compress(b"hello world")
    url = f"/sources/{source.id}"

    opened = client.get(f"{url}/open", params={"path": "app.log.GZ"})
    downloaded = client.get(f"{url}/download", params={"path": "app.log.GZ"})

    assert opened.status_code == 200 and opened.content == b"hello world"
    assert downloaded.status_code == 200 and downloaded.content == b"hello world"
    assert 'filename="app.log"' in downloaded.headers["content-disposition"]


def test_folder_zip_without_skipped_archives_has_no_skipped_list(app_client, session, tmp_path):
    app, client = app_client
    (tmp_path / "app.log").write_text("hello")
    (tmp_path / "clean.zip").write_bytes(_zip_bytes({"a.log": "a", "secret.txt": "x"}))
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*.log")
    _add_rule(session, source, 1, RuleType.include, "**/*.zip")

    response = client.get(f"/sources/{source.id}/download-zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert set(zf.namelist()) == {"app.log", "clean.zip"}
    event = session.exec(select(AuditLog).where(AuditLog.action == "folder.download")).one()
    assert "skipped_archives" not in event.event_metadata


def test_folder_zip_skipped_list_avoids_an_existing_skipped_txt(app_client, session, tmp_path):
    app, client = app_client
    (tmp_path / "SKIPPED.txt").write_text("the folder's own file")
    (tmp_path / "hiding.zip").write_bytes(_zip_bytes({"secret.txt": "x"}))
    source = _make_local_source(session, tmp_path)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, "**/secret.txt")

    response = client.get(f"/sources/{source.id}/download-zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert set(zf.namelist()) == {"SKIPPED.txt", "SKIPPED (PerchTail).txt"}
        assert zf.read("SKIPPED.txt") == b"the folder's own file"
        assert zf.read("SKIPPED (PerchTail).txt") == (
            b"hiding.zip: can't be included whole; open or download its files one by one\n"
        )


def _tar_gz_with_links() -> bytes:
    """app.log and secret.txt as regular files, plus a symbolic and a hard
    link with visible names, both pointing at secret.txt."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in (("app.log", b"hello"), ("secret.txt", b"nope")):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        for name, kind in (("link.log", tarfile.SYMTYPE), ("hard.log", tarfile.LNKTYPE)):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = "secret.txt"
            tf.addfile(info)
    return buf.getvalue()


def _zip_with_link() -> bytes:
    """The zip equivalent: link.log stored with a symbolic link's mode, whose
    content (the link's target) is "secret.txt"."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("app.log", "hello")
        zf.writestr("secret.txt", "nope")
        link = zipfile.ZipInfo("link.log")
        link.external_attr = (stat_module.S_IFLNK | 0o777) << 16
        zf.writestr(link, "secret.txt")
    return buf.getvalue()


def _links_source(session, app, fake_sftp, archive: str, content: bytes) -> Source:
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    _add_rule(session, source, 1, RuleType.exclude, f"{archive}/secret.txt")
    fake_sftp.files[f"/var/log/appname/{archive}"] = content
    return source


def test_tar_links_are_not_listed_and_cannot_be_opened(
    app_client, session, fake_sftp, scratch_store
):
    app, client = app_client
    archive = "logs.tar.gz"
    source = _links_source(session, app, fake_sftp, archive, _tar_gz_with_links())
    url = f"/sources/{source.id}"

    listed = client.get(f"{url}/browse", params={"path": archive})
    assert listed.status_code == 200
    assert [e["name"] for e in listed.json()] == ["app.log"]

    for member in ("link.log", "hard.log"):
        for endpoint in ("open", "download"):
            response = client.get(f"{url}/{endpoint}", params={"path": archive, "member": member})
            assert response.status_code == 404, (member, endpoint)
            assert b"nope" not in response.content

    opened = client.get(f"{url}/open", params={"path": archive, "member": "app.log"})
    assert opened.status_code == 200 and opened.content == b"hello"


@pytest.mark.parametrize("excluded", [True, False])
def test_whole_tar_with_an_excluded_link_is_refused(
    app_client, session, fake_sftp, scratch_store, excluded
):
    """A link is never listed or opened, but the whole archive still carries
    its name and target, so an excluded link refuses the whole archive like
    an excluded file would."""
    app, client = app_client
    archive = "a.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("app.log")
        info.size = len(b"hello")
        tf.addfile(info, io.BytesIO(b"hello"))
        link = tarfile.TarInfo("secret.log")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/hostname"
        tf.addfile(link)
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**")
    if excluded:
        _add_rule(session, source, 1, RuleType.exclude, "**/secret.log")
    fake_sftp.files[f"/var/log/appname/{archive}"] = buf.getvalue()

    whole = client.get(f"/sources/{source.id}/download", params={"path": archive})

    if excluded:
        assert whole.status_code == 403
        assert whole.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
    else:
        assert whole.status_code == 200 and whole.content == buf.getvalue()


def test_a_member_that_is_not_in_the_archive_is_a_404(
    app_client, session, fake_sftp, scratch_store
):
    app, client = app_client
    source = _links_source(session, app, fake_sftp, "logs.tar.gz", _tar_gz_with_links())
    fake_sftp.files["/var/log/appname/logs.zip"] = _zip_with_link()

    for archive in ("logs.tar.gz", "logs.zip"):
        response = client.get(
            f"/sources/{source.id}/open", params={"path": archive, "member": "missing.log"}
        )
        assert response.status_code == 404, archive


def test_zip_links_are_read_as_stored(app_client, session, fake_sftp, scratch_store):
    """zipfile never follows a link: a link member opens as the text of its
    target's name, as before, never as the target's content."""
    app, client = app_client
    archive = "logs.zip"
    source = _links_source(session, app, fake_sftp, archive, _zip_with_link())
    url = f"/sources/{source.id}"

    listed = client.get(f"{url}/browse", params={"path": archive})
    assert [e["name"] for e in listed.json()] == ["app.log", "link.log"]
    opened = client.get(f"{url}/open", params={"path": archive, "member": "link.log"})
    assert opened.status_code == 200 and opened.content == b"secret.txt"
