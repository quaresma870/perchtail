import io

import app.collectors.smb as smb_module
import pytest
from app.models import PatternKind, Protocol, Rule, RuleType, Source


class FakeSMBInfo:
    def __init__(self, size: int):
        self.end_of_file = size


class FakeSMBDirEntry:
    def __init__(self, name: str, is_dir: bool, size: int = 0):
        self.name = name
        self._is_dir = is_dir
        self.smb_info = FakeSMBInfo(size)

    def is_dir(self) -> bool:
        return self._is_dir


class FakeSMBClient:
    def __init__(self):
        self.sessions: list[tuple] = []
        self.listing: list[FakeSMBDirEntry] = []
        self.files: dict[str, bytes] = {}
        self.requested_paths: list[str] = []
        self.scandir_kwargs: list[dict] = []
        self.open_file_kwargs: list[dict] = []

    def register_session(self, host, username=None, password=None, port=None, auth_protocol=None):
        self.sessions.append((host, username, password, port, auth_protocol))

    def scandir(self, path, **kwargs):
        self.requested_paths.append(path)
        self.scandir_kwargs.append(kwargs)
        return self.listing

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

    rules = [_rule(0, RuleType.include, "*.log")]
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
