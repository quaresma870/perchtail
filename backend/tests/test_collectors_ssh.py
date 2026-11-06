import io
import posixpath
import stat as stat_module
from pathlib import Path
from types import SimpleNamespace

import app.collectors.ssh as ssh_module
import paramiko
import pytest
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from structlog.testing import capture_logs


class FakeAttr:
    def __init__(self, filename: str, is_dir: bool, size: int = 0, is_link: bool = False):
        self.filename = filename
        if is_link:
            self.st_mode = stat_module.S_IFLNK
        else:
            self.st_mode = stat_module.S_IFDIR if is_dir else stat_module.S_IFREG
        self.st_size = size


class FakeSFTPClient:
    """`symlinks` maps a link's absolute path to its target; normalize()
    resolves them like the server's SSH_FXP_REALPATH would. `dirs` lists
    absolute paths that stat() reports as directories."""

    def __init__(self, listing=None, files=None, symlinks=None, dirs=None):
        self._listing = listing or []
        self._files = files or {}
        self._symlinks = symlinks or {}
        self._dirs = set(dirs or [])
        self.closed = False
        self.requested_paths = []
        self.fetched_paths = []

    def normalize(self, path):
        path = posixpath.normpath(path)
        parts = path.strip("/").split("/") if path != "/" else []
        resolved = "/"
        for part in parts:
            resolved = posixpath.join(resolved, part)
            seen = 0
            while resolved in self._symlinks and seen < 10:
                resolved = posixpath.normpath(self._symlinks[resolved])
                seen += 1
        return resolved

    def listdir_attr(self, path):
        self.requested_paths.append(path)
        return self._listing

    def get(self, remote_path, local_path):
        self.fetched_paths.append(remote_path)
        Path(local_path).write_bytes(self._files[remote_path])

    def stat(self, remote_path):
        name = remote_path.rsplit("/", 1)[-1]
        if remote_path in self._dirs:
            return FakeAttr(name, True)
        if remote_path not in self._files:
            raise FileNotFoundError(remote_path)
        return FakeAttr(name, False, len(self._files[remote_path]))

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


def _patch_ssh_infra(monkeypatch, tmp_path, *, connect_raises=None, policy="tofu"):
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
        lambda: SimpleNamespace(
            ssh_known_hosts_path=str(tmp_path / "known_hosts"), ssh_host_key_policy=policy
        ),
    )
    created: list[FakeParamikoClient] = []

    def _factory():
        client = FakeParamikoClient()
        client.connect_raises = connect_raises
        created.append(client)
        return client

    monkeypatch.setattr(ssh_module.paramiko, "SSHClient", _factory)
    return created


def test_connect_trusts_on_first_use_by_default(monkeypatch, tmp_path):
    created = _patch_ssh_infra(monkeypatch, tmp_path)
    ssh_module._connect(_source())
    assert isinstance(created[0].policy, ssh_module.TrustOnFirstUsePolicy)


def test_strict_mode_rejects_hosts_missing_from_known_hosts(monkeypatch, tmp_path):
    created = _patch_ssh_infra(monkeypatch, tmp_path, policy="strict")
    ssh_module._connect(_source())
    assert isinstance(created[0].policy, paramiko.RejectPolicy)


def test_trust_on_first_use_pins_the_key_and_logs_its_fingerprint():
    client = paramiko.SSHClient()
    key = paramiko.ECDSAKey.generate()

    with capture_logs() as logs:
        ssh_module.TrustOnFirstUsePolicy().missing_host_key(client, "app01.example.com", key)

    assert client.get_host_keys().lookup("app01.example.com")[key.get_name()] == key
    pinned = [entry for entry in logs if entry["event"] == "ssh.host_key_pinned"]
    assert pinned and pinned[0]["fingerprint"] == ssh_module.fingerprint(key)
    assert pinned[0]["fingerprint"].startswith("SHA256:")


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
        ssh_module,
        "get_settings",
        lambda: SimpleNamespace(ssh_known_hosts_path=str(nested), ssh_host_key_policy="tofu"),
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

    rules = [_rule(0, RuleType.include, "**/*.log")]
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


def _openssh_private_key(private_key, password: bytes | None = None) -> str:
    from cryptography.hazmat.primitives import serialization

    encryption = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    return private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, encryption
    ).decode()


def _generated_key(kind: str) -> str:
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    if kind == "ed25519":
        return _openssh_private_key(ed25519.Ed25519PrivateKey.generate())
    if kind == "ecdsa":
        return _openssh_private_key(ec.generate_private_key(ec.SECP256R1()))
    return _openssh_private_key(rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("ed25519", paramiko.Ed25519Key),
        ("ecdsa", paramiko.ECDSAKey),
        ("rsa", paramiko.RSAKey),
    ],
)
def test_connect_accepts_every_supported_private_key_type(monkeypatch, tmp_path, kind, expected):
    created = _patch_ssh_infra(monkeypatch, tmp_path)
    key_text = _generated_key(kind)
    monkeypatch.setattr(
        ssh_module, "decrypt_credential", lambda _ref: {"username": "svc", "private_key": key_text}
    )

    ssh_module._connect(_source())

    assert isinstance(created[0].connect_calls[0][2]["pkey"], expected)


def test_unreadable_private_key_is_a_clear_error():
    with pytest.raises(ValueError, match="not a supported SSH private key"):
        ssh_module.load_private_key("not a key at all")


def test_passphrase_protected_private_key_is_a_clear_error():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key_text = _openssh_private_key(ed25519.Ed25519PrivateKey.generate(), b"secret")
    with pytest.raises(ValueError, match="passphrase"):
        ssh_module.load_private_key(key_text)


def test_fetch_refuses_a_file_over_max_fetch_mb_before_transferring_it(monkeypatch, tmp_path):
    from app.collectors.base import FetchTooLargeError

    sftp = FakeSFTPClient(files={"/var/log/appname/big.log": b"x" * 2048})
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    monkeypatch.setattr("app.collectors.base.max_fetch_bytes", lambda: 1024)

    destination = tmp_path / "out"
    with pytest.raises(FetchTooLargeError):
        ssh_module.fetch_file(_source(), "big.log", destination)
    assert not destination.exists()


# --- containment: symlinks must not lead outside base_path (lab F-10) ------


def _escape_fixture():
    return FakeSFTPClient(
        listing=[
            FakeAttr("app.log", False, 5),
            FakeAttr("escape", False, 4, is_link=True),
            FakeAttr("inner.log", False, 9, is_link=True),
            FakeAttr("dangling", False, 4, is_link=True),
        ],
        files={
            "/var/log/appname/app.log": b"hello",
            "/etc/passwd": b"root:x:0:0",
        },
        symlinks={
            "/var/log/appname/escape": "/etc",
            "/var/log/appname/inner.log": "/var/log/appname/app.log",
            "/var/log/appname/dangling": "/var/log/appname/missing",
        },
        dirs={"/etc", "/var/log/appname"},
    )


def test_fetch_through_a_symlink_outside_base_path_is_refused(monkeypatch, tmp_path):
    sftp = _escape_fixture()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    with pytest.raises(ssh_module.PathOutsideBaseError):
        ssh_module.fetch_file(_source(), "escape/passwd", tmp_path / "out")

    assert sftp.fetched_paths == []
    assert not (tmp_path / "out").exists()


def test_browse_into_a_symlinked_dir_outside_base_path_is_refused(monkeypatch):
    sftp = _escape_fixture()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    with pytest.raises(ssh_module.PathOutsideBaseError):
        ssh_module.list_directory(_source(), [_rule(0, RuleType.include, "**/*")], "escape")

    assert sftp.requested_paths == []


def test_listing_hides_outward_and_dangling_symlinks_and_resolves_inward_ones(monkeypatch):
    sftp = _escape_fixture()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    entries = ssh_module.list_directory(_source(), [_rule(0, RuleType.include, "**/*")])

    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"app.log", "inner.log"}
    # An inward link is shown as what it points to.
    assert by_name["inner.log"].is_dir is False
    assert by_name["inner.log"].size == 5


def test_fetch_through_an_inward_symlink_reads_the_resolved_target(monkeypatch, tmp_path):
    sftp = _escape_fixture()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    ssh_module.fetch_file(_source(), "inner.log", tmp_path / "out")

    assert (tmp_path / "out").read_bytes() == b"hello"
    # The resolved real path is what gets read, not the link's own path.
    assert sftp.fetched_paths == ["/var/log/appname/app.log"]


def test_a_symlinked_base_path_is_resolved_before_comparing(monkeypatch, tmp_path):
    sftp = FakeSFTPClient(
        files={"/data/real/app.log": b"ok"},
        symlinks={"/var/log/appname": "/data/real"},
        dirs={"/data/real"},
    )
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    ssh_module.fetch_file(_source(), "app.log", tmp_path / "out")

    assert (tmp_path / "out").read_bytes() == b"ok"


def test_chroot_base_path_of_slash_allows_everything_inside_the_chroot(monkeypatch, tmp_path):
    sftp = FakeSFTPClient(files={"/app/app.log": b"x"})
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    ssh_module.fetch_file(_source(base_path="/"), "app/app.log", tmp_path / "out")

    assert (tmp_path / "out").read_bytes() == b"x"


def test_sibling_directory_with_a_shared_prefix_is_outside(monkeypatch, tmp_path):
    # /var/log/appname-other must not count as inside /var/log/appname.
    sftp = FakeSFTPClient(
        files={"/var/log/appname-other/secret.log": b"s"},
        symlinks={"/var/log/appname/x": "/var/log/appname-other"},
    )
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    with pytest.raises(ssh_module.PathOutsideBaseError):
        ssh_module.fetch_file(_source(), "x/secret.log", tmp_path / "out")


# --- incremental follow (#254) ----------------------------------------------


class _SeekableRemoteFile(io.BytesIO):
    """What paramiko's SFTPFile offers read_range: stat, seek and read,
    recording the offset so a test can tell nothing before it was read."""

    def __init__(self, content: bytes, reads: list):
        super().__init__(content)
        self._reads = reads

    def stat(self):
        return FakeAttr("app.log", False, len(self.getvalue()))

    def read(self, size=-1):
        self._reads.append((self.tell(), size))
        return super().read(size)


class _RangeSFTPClient(FakeSFTPClient):
    def __init__(self, files):
        super().__init__(files=files)
        self.reads: list = []

    def open(self, path, mode="r"):
        assert mode == "rb"
        return _SeekableRemoteFile(self._files[path], self.reads)


def test_read_range_reads_only_from_the_offset(monkeypatch):
    sftp = _RangeSFTPClient({"/var/log/appname/app.log": b"line one\nline two\n"})
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    assert ssh_module.read_range(_source(), "app.log", 9, 1000) == (18, b"line two\n")
    assert sftp.reads == [(9, 9)]
    assert ssh_module.read_range(_source(), "app.log", 18, 1000) == (18, b"")
    assert sftp.closed


def test_read_range_refuses_a_link_out_of_the_base(monkeypatch):
    sftp = _RangeSFTPClient({"/etc/shadow": b"secret"})
    sftp._symlinks = {"/var/log/appname/app.log": "/etc/shadow"}
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    with pytest.raises(ssh_module.PathOutsideBaseError):
        ssh_module.read_range(_source(), "app.log", 0, 10)
