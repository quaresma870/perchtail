import stat as stat_module
from pathlib import Path
from types import SimpleNamespace

import app.collectors.ssh as ssh_module
import paramiko
import pytest
from app.models import PatternKind, Protocol, Rule, RuleType, Source


class FakeAttr:
    def __init__(self, filename: str, is_dir: bool, size: int = 0):
        self.filename = filename
        self.st_mode = stat_module.S_IFDIR if is_dir else stat_module.S_IFREG
        self.st_size = size


class FakeSFTPClient:
    def __init__(self, listing=None, files=None):
        self._listing = listing or []
        self._files = files or {}
        self.closed = False
        self.requested_paths = []

    def listdir_attr(self, path):
        self.requested_paths.append(path)
        return self._listing

    def get(self, remote_path, local_path):
        Path(local_path).write_bytes(self._files[remote_path])

    def close(self):
        self.closed = True


class FakeSSHClient:
    def __init__(self, sftp: FakeSFTPClient):
        self._sftp = sftp
        self.closed = False

    def open_sftp(self):
        return self._sftp

    def close(self):
        self.closed = True


def _source(**overrides) -> Source:
    defaults = dict(
        name="app01",
        protocol=Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log/appname",
        credential_ref="encrypted-blob",
    )
    defaults.update(overrides)
    return Source(**defaults)


def _rule(order, type_, pattern, kind=PatternKind.glob) -> Rule:
    return Rule(source_id=1, order=order, type=type_, pattern=pattern, pattern_kind=kind)


def test_connect_raises_without_private_key_or_password(monkeypatch):
    monkeypatch.setattr(ssh_module, "decrypt_credential", lambda _ref: {"username": "svc"})
    with pytest.raises(ValueError):
        ssh_module._connect(_source())


class FakeParamikoClient:
    """Stand-in for paramiko.SSHClient that records what _connect() does
    with it, without touching the network -- a new instance is created
    per _connect() call in production code, same here."""

    def __init__(self):
        self.policy = None
        self.loaded_from: list[str] = []
        self.saved_to: list[str] = []
        self.connect_calls: list[tuple] = []
        self.connect_raises: Exception | None = None

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def load_host_keys(self, path):
        self.loaded_from.append(path)

    def connect(self, hostname, port=22, **kwargs):
        self.connect_calls.append((hostname, port, kwargs))
        if self.connect_raises is not None:
            raise self.connect_raises

    def save_host_keys(self, path):
        self.saved_to.append(path)

    def close(self):
        pass


def _patch_ssh_infra(monkeypatch, tmp_path, *, connect_raises=None):
    """Patches paramiko.SSHClient to FakeParamikoClient (registering every
    created instance so a test can inspect it afterward) and points
    ssh_known_hosts_path at a fresh tmp_path location. Returns the list of
    created fake clients, in creation order."""
    monkeypatch.setattr(
        ssh_module, "decrypt_credential", lambda _ref: {"username": "svc", "password": "pw"}
    )
    monkeypatch.setattr(
        ssh_module,
        "get_settings",
        lambda: SimpleNamespace(ssh_known_hosts_path=str(tmp_path / "known_hosts")),
    )
    created: list[FakeParamikoClient] = []

    def _factory():
        client = FakeParamikoClient()
        client.connect_raises = connect_raises
        created.append(client)
        return client

    monkeypatch.setattr(ssh_module.paramiko, "SSHClient", _factory)
    return created


def test_connect_uses_autoadd_policy(monkeypatch, tmp_path):
    created = _patch_ssh_infra(monkeypatch, tmp_path)
    ssh_module._connect(_source())
    assert isinstance(created[0].policy, paramiko.AutoAddPolicy)


def test_connect_skips_loading_a_known_hosts_file_that_does_not_exist_yet(monkeypatch, tmp_path):
    created = _patch_ssh_infra(monkeypatch, tmp_path)
    ssh_module._connect(_source())
    assert created[0].loaded_from == []
    assert created[0].saved_to == [str(tmp_path / "known_hosts")]


def test_connect_loads_previously_saved_host_keys_on_a_later_call(monkeypatch, tmp_path):
    created = _patch_ssh_infra(monkeypatch, tmp_path)
    ssh_module._connect(_source())
    # save_host_keys is faked (no real file write), so create the file by
    # hand to simulate the first call having actually persisted something.
    (tmp_path / "known_hosts").write_text("app01.example.com ssh-ed25519 AAAA...\n")

    ssh_module._connect(_source())

    assert created[1].loaded_from == [str(tmp_path / "known_hosts")]


def test_connect_creates_the_known_hosts_parent_directory(monkeypatch, tmp_path):
    nested = tmp_path / "nested" / "dir" / "known_hosts"
    monkeypatch.setattr(
        ssh_module, "decrypt_credential", lambda _ref: {"username": "svc", "password": "pw"}
    )
    monkeypatch.setattr(
        ssh_module, "get_settings", lambda: SimpleNamespace(ssh_known_hosts_path=str(nested))
    )
    monkeypatch.setattr(ssh_module.paramiko, "SSHClient", FakeParamikoClient)

    ssh_module._connect(_source())

    assert nested.parent.is_dir()


def test_connect_propagates_bad_host_key_exception_without_saving(monkeypatch, tmp_path):
    mismatch = paramiko.BadHostKeyException("app01.example.com", object(), object())
    created = _patch_ssh_infra(monkeypatch, tmp_path, connect_raises=mismatch)

    with pytest.raises(paramiko.BadHostKeyException):
        ssh_module._connect(_source())

    # A key mismatch is a real MITM-detection signal -- nothing should be
    # (re)written to disk when connect() itself rejected the host.
    assert created[0].saved_to == []


def test_list_directory_always_shows_directories_but_filters_files(monkeypatch):
    listing = [
        FakeAttr("nested", is_dir=True),
        FakeAttr("app.log", is_dir=False, size=100),
        FakeAttr("secret.txt", is_dir=False, size=5),
    ]
    fake_sftp = FakeSFTPClient(listing=listing)
    fake_client = FakeSSHClient(fake_sftp)
    monkeypatch.setattr(ssh_module, "_connect", lambda source: fake_client)

    rules = [_rule(0, RuleType.include, "*.log")]
    entries = ssh_module.list_directory(_source(), rules)

    names = {e.name: e for e in entries}
    assert "nested" in names and names["nested"].is_dir is True
    assert "app.log" in names and names["app.log"].size == 100
    assert "secret.txt" not in names
    assert fake_sftp.closed is True
    assert fake_client.closed is True


def test_list_directory_builds_relative_paths_for_nested_calls(monkeypatch):
    listing = [FakeAttr("debug.log", is_dir=False, size=10)]
    fake_sftp = FakeSFTPClient(listing=listing)
    fake_client = FakeSSHClient(fake_sftp)
    monkeypatch.setattr(ssh_module, "_connect", lambda source: fake_client)

    rules = [_rule(0, RuleType.include, "nested/*.log")]
    entries = ssh_module.list_directory(_source(), rules, relative_path="nested")

    assert entries[0].path == "nested/debug.log"
    assert fake_sftp.requested_paths == ["/var/log/appname/nested"]


def test_list_directory_queries_base_path_when_no_relative_path(monkeypatch):
    fake_sftp = FakeSFTPClient(listing=[])
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(fake_sftp))

    ssh_module.list_directory(_source(), [])
    assert fake_sftp.requested_paths == ["/var/log/appname"]


def test_fetch_file_writes_remote_content_to_destination(monkeypatch, tmp_path):
    fake_sftp = FakeSFTPClient(files={"/var/log/appname/app.log": b"hello world"})
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(fake_sftp))

    destination = tmp_path / "scratch-file"
    ssh_module.fetch_file(_source(), "app.log", destination)

    assert destination.read_bytes() == b"hello world"
