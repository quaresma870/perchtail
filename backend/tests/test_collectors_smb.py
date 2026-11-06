import io
from types import SimpleNamespace

import app.collectors.smb as smb_module
import pytest
from app.models import PatternKind, Protocol, Rule, RuleType, Source

REPARSE = 0x400


class FakeSMBInfo:
    def __init__(self, size: int, attributes: int):
        self.end_of_file = size
        self.file_attributes = attributes


class FakeSMBDirEntry:
    def __init__(self, name: str, is_dir: bool, size: int = 0, reparse: bool = False):
        self.name = name
        self._is_dir = is_dir
        self._reparse = reparse
        self.smb_info = FakeSMBInfo(size, (0x10 if is_dir else 0x80) | (REPARSE if reparse else 0))

    def is_dir(self) -> bool:
        if self._reparse:
            # What smbclient does for a symlink it can't redirect.
            raise OSError("SMBLinkRedirectionError: cannot be redirected")
        return self._is_dir


class FakeSMBClient:
    def __init__(self):
        self.sessions: list[tuple] = []
        self.listing: list[FakeSMBDirEntry] = []
        self.files: dict[str, bytes] = {}
        self.stat_sizes: dict[str, int] = {}
        self.stat_kwargs: list[dict] = []
        self.requested_paths: list[str] = []
        self.scandir_kwargs: list[dict] = []
        self.open_file_kwargs: list[dict] = []
        self.reparse_paths: set[str] = set()
        self.caches: list[dict] = []
        self.reset_caches: list[dict] = []

    def register_session(
        self,
        host,
        username=None,
        password=None,
        port=None,
        auth_protocol=None,
        connection_cache=None,
    ):
        self.sessions.append((host, username, password, port, auth_protocol))
        self.caches.append(connection_cache)

    def reset_connection_cache(self, fail_on_error=True, connection_cache=None):
        self.reset_caches.append(connection_cache)

    def lstat(self, path, **kwargs):
        return SimpleNamespace(st_file_attributes=REPARSE if path in self.reparse_paths else 0x10)

    def scandir(self, path, **kwargs):
        self.requested_paths.append(path)
        self.scandir_kwargs.append(kwargs)
        return self.listing

    def stat(self, path, **kwargs):
        self.stat_kwargs.append(kwargs)
        if path in self.stat_sizes:
            return SimpleNamespace(st_size=self.stat_sizes[path])
        return SimpleNamespace(st_size=len(self.files[path]))

    def open_file(self, path, mode="rb", **kwargs):
        self.requested_paths.append(path)
        self.open_file_kwargs.append(kwargs)
        return io.BytesIO(self.files[path])


@pytest.fixture()
def fake_smbclient(monkeypatch):
    fake = FakeSMBClient()
    monkeypatch.setattr(smb_module, "smbclient", fake)
    monkeypatch.setattr(
        smb_module, "decrypt_credential", lambda ref: {"username": "svc", "password": "s3cret"}
    )
    return fake


def _source(**overrides) -> Source:
    defaults = dict(
        name="app01",
        protocol=Protocol.smb,
        host="fileserver.example.com",
        base_path="AppLogs",
        credential_ref="encrypted",
    )
    defaults.update(overrides)
    return Source(**defaults)


def _rule(order, type_, pattern, kind=PatternKind.glob) -> Rule:
    return Rule(source_id=1, order=order, type=type_, pattern=pattern, pattern_kind=kind)


def test_unc_path_builds_expected_share_path():
    source = _source()
    assert smb_module._unc_path(source) == "\\\\fileserver.example.com\\AppLogs"
    assert (
        smb_module._unc_path(source, "nested/app.log")
        == "\\\\fileserver.example.com\\AppLogs\\nested\\app.log"
    )


def test_list_directory_registers_session_and_filters_files(fake_smbclient):
    fake_smbclient.listing = [
        FakeSMBDirEntry("nested", is_dir=True),
        FakeSMBDirEntry("app.log", is_dir=False, size=100),
        FakeSMBDirEntry("secret.txt", is_dir=False, size=5),
    ]

    rules = [_rule(0, RuleType.include, "**/*.log")]
    entries = {e.name: e for e in smb_module.list_directory(_source(), rules)}

    assert "nested" in entries and entries["nested"].is_dir is True
    assert "app.log" in entries and entries["app.log"].size == 100
    assert "secret.txt" not in entries
    assert fake_smbclient.sessions[0][0] == "fileserver.example.com"


def test_connect_kwargs_pins_ntlm_auth_protocol(fake_smbclient):
    """See _connect_kwargs's docstring -- credential_ref never carries
    Kerberos credentials, so leaving smbclient's default "negotiate" in
    place can fail the whole SPNEGO negotiation on a client with no
    Kerberos configuration at all, instead of falling back to NTLM."""
    kwargs = smb_module._connect_kwargs(_source())
    assert kwargs["auth_protocol"] == "ntlm"


def test_non_default_port_reaches_scandir_and_open_file_not_just_register_session(
    fake_smbclient, tmp_path
):
    """smbclient's own scandir()/open_file() resolve their session via
    get_smb_tree(path, port=445, ...) -- a *separate* default from whatever
    was passed to register_session() -- so a source pinned to a
    non-standard port must still see that port on every call, not just the
    initial register_session()."""
    fake_smbclient.listing = [FakeSMBDirEntry("app.log", is_dir=False, size=1)]
    fake_smbclient.files["\\\\fileserver.example.com\\AppLogs\\app.log"] = b"x"
    source = _source(port=1445)

    smb_module.list_directory(source, [_rule(0, RuleType.include, "*.log")])
    assert fake_smbclient.sessions[0][3] == 1445
    assert fake_smbclient.scandir_kwargs[0]["port"] == 1445

    smb_module.fetch_file(source, "app.log", tmp_path / "out")
    assert fake_smbclient.open_file_kwargs[0]["port"] == 1445


def test_list_directory_builds_relative_paths_for_nested_calls(fake_smbclient):
    fake_smbclient.listing = [FakeSMBDirEntry("debug.log", is_dir=False, size=10)]

    rules = [_rule(0, RuleType.include, "nested/*.log")]
    entries = smb_module.list_directory(_source(), rules, relative_path="nested")

    assert entries[0].path == "nested/debug.log"
    assert fake_smbclient.requested_paths == ["\\\\fileserver.example.com\\AppLogs\\nested"]


def test_fetch_file_writes_remote_content_to_destination(fake_smbclient, tmp_path):
    fake_smbclient.files["\\\\fileserver.example.com\\AppLogs\\app.log"] = b"hello world"

    destination = tmp_path / "out"
    smb_module.fetch_file(_source(), "app.log", destination)

    assert destination.read_bytes() == b"hello world"


def test_local_copy_fetches_into_a_temp_path(fake_smbclient):
    fake_smbclient.files["\\\\fileserver.example.com\\AppLogs\\app.log"] = b"hello world"

    with smb_module.local_copy(_source(), "app.log") as path:
        assert path.read_bytes() == b"hello world"


def test_list_directory_applies_rules_case_insensitively(fake_smbclient):
    fake_smbclient.listing = [
        FakeSMBDirEntry("SECRET.TXT", is_dir=False, size=3),
        FakeSMBDirEntry("App.Log", is_dir=False, size=3),
    ]
    rules = [
        Rule(source_id=1, order=0, type=RuleType.include, pattern="*.log"),
        Rule(source_id=1, order=1, type=RuleType.include, pattern="*.txt"),
        Rule(source_id=1, order=2, type=RuleType.exclude, pattern="secret.txt"),
    ]
    names = [e.name for e in smb_module.list_directory(_source(), rules)]
    assert names == ["App.Log"]


def test_fetch_file_stops_at_max_fetch_mb(fake_smbclient, tmp_path, monkeypatch):
    from app.collectors.base import FetchTooLargeError

    monkeypatch.setattr("app.collectors.base.max_fetch_bytes", lambda: 8)
    fake_smbclient.files[smb_module._unc_path(_source(), "big.log")] = b"x" * 100

    destination = tmp_path / "out"
    with pytest.raises(FetchTooLargeError):
        smb_module.fetch_file(_source(), "big.log", destination)
    assert not destination.exists()


def test_fetch_opens_with_full_share_access_so_live_logs_can_be_read(fake_smbclient, tmp_path):
    """#166: without share_access the open asked for exclusive access and a
    log another process had open for writing failed with a sharing
    violation."""
    fake_smbclient.files[smb_module._unc_path(_source(), "app.log")] = b"live"

    smb_module.fetch_file(_source(), "app.log", tmp_path / "out")

    assert fake_smbclient.open_file_kwargs[0]["share_access"] == "rwd"
    assert (tmp_path / "out").read_bytes() == b"live"


def test_fetch_refuses_an_oversized_file_before_opening_it(fake_smbclient, tmp_path, monkeypatch):
    """#167: the cap used to be enforced only after downloading up to it."""
    from app.collectors.base import FetchTooLargeError

    monkeypatch.setattr("app.collectors.base.max_fetch_bytes", lambda: 8)
    path = smb_module._unc_path(_source(), "big.log")
    fake_smbclient.files[path] = b"x"
    fake_smbclient.stat_sizes[path] = 100

    with pytest.raises(FetchTooLargeError):
        smb_module.fetch_file(_source(), "big.log", tmp_path / "out")

    assert fake_smbclient.open_file_kwargs == []
    assert fake_smbclient.stat_kwargs[0]["port"] == 445


def test_every_request_authenticates_with_its_own_session(fake_smbclient, tmp_path):
    """Two sources for the same host and username used to share smbclient's
    global session pool, so one with the wrong password read with the
    other's authenticated session."""
    fake_smbclient.listing = [FakeSMBDirEntry("app.log", is_dir=False, size=1)]
    fake_smbclient.files[smb_module._unc_path(_source(), "app.log")] = b"x"

    smb_module.list_directory(_source(), [_rule(0, RuleType.include, "**")])
    smb_module.fetch_file(_source(), "app.log", tmp_path / "out")

    first, second = fake_smbclient.caches
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first is not second
    assert fake_smbclient.reset_caches == [first, second]
    assert all(kw["connection_cache"] is first for kw in fake_smbclient.scandir_kwargs)
    assert fake_smbclient.open_file_kwargs[0]["connection_cache"] is second


def test_junctions_and_symlinks_are_left_out_of_listings(fake_smbclient):
    """A reparse point used to be listed (and followed by the server) or,
    for a symlink smbclient can't redirect, fail the whole listing."""
    fake_smbclient.listing = [
        FakeSMBDirEntry("jx", is_dir=True, reparse=True),
        FakeSMBDirEntry("sx", is_dir=True, reparse=True),
        FakeSMBDirEntry("app.log", is_dir=False, size=1),
    ]
    entries = smb_module.list_directory(_source(), [_rule(0, RuleType.include, "**")])
    assert [e.name for e in entries] == ["app.log"]


@pytest.mark.parametrize("path", ["jx", "jx/secret.txt", "jx/deeper/secret.txt"])
def test_paths_through_a_reparse_point_are_refused(fake_smbclient, tmp_path, path):
    from app.collectors.base import PathOutsideBaseError

    fake_smbclient.reparse_paths = {smb_module._unc_path(_source(), "jx")}
    fake_smbclient.files[smb_module._unc_path(_source(), path)] = b"FORA DA SHARE"

    with pytest.raises(PathOutsideBaseError):
        smb_module.list_directory(_source(), [_rule(0, RuleType.include, "**")], path)
    with pytest.raises(PathOutsideBaseError):
        smb_module.fetch_file(_source(), path, tmp_path / "out")
    assert fake_smbclient.open_file_kwargs == []
    assert len(fake_smbclient.reset_caches) == 2


# --- incremental follow (#254) ----------------------------------------------


def test_read_range_reads_from_the_offset_with_full_share_access(fake_smbclient):
    path = smb_module._unc_path(_source(), "app.log")
    fake_smbclient.files[path] = b"line one\nline two\n"

    assert smb_module.read_range(_source(), "app.log", 9, 1000) == (18, b"line two\n")
    assert smb_module.read_range(_source(), "app.log", 30, 1000) == (18, b"")
    assert fake_smbclient.open_file_kwargs[0]["share_access"] == "rwd"
