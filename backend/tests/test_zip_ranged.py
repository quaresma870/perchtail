"""Ranged reads of a remote .zip (collectors/ranged.py): what listing an
archive and extracting a member transfer, the block cache and the read
ahead, the MAX_FETCH_MB budget, and each remote connector's open_ranged
(SSH, SMB, WinRM over WinRS and behind JEA) with the checks fetch_file
makes."""

import base64
import io
import os
import re
import zipfile
from pathlib import Path

import app.collectors.smb as smb_module
import app.collectors.ssh as ssh_module
import app.collectors.winrm as winrm_module
import pytest
from app.archives import extract_member, scan_members
from app.collectors.base import (
    FetchTooLargeError,
    LinkTargetHiddenError,
    PathOutsideBaseError,
    SourceNotAFileError,
    SourceUnreachableError,
)
from app.collectors.ranged import (
    BLOCK_BYTES,
    READAHEAD_MAX,
    READAHEAD_START,
    RangedFile,
    RemoteFileChangedError,
    check_length,
    check_size,
    ranged_file,
)
from app.collectors.registry import CONNECTORS
from app.models import PatternKind, Protocol, Rule, RuleType, Source

from tests.test_collectors_smb import _source as smb_source
from tests.test_collectors_smb import fake_smbclient  # noqa: F401 (fixture)
from tests.test_collectors_ssh import FakeSFTPClient, FakeSSHClient
from tests.test_collectors_ssh import _RemoteFile as SSHRemoteFile
from tests.test_collectors_ssh import _source as ssh_source
from tests.test_collectors_winrm import _fault
from tests.test_collectors_winrm_jea import FILE as JEA_FILE
from tests.test_collectors_winrm_jea import _source as jea_source
from tests.test_collectors_winrm_jea import endpoint  # noqa: F401 (fixture)

MIB = 1024 * 1024
APP_LOG = b"2026-11-23 10:00:00 INFO a line of the log\n" * 500
SHOW_ALL = [
    Rule(source_id=1, order=0, type=RuleType.include, pattern="**", pattern_kind=PatternKind.glob)
]


class _SparseWriter(io.RawIOBase):
    """A file that leaves runs of zeros as holes, so an archive with a
    300 MB member takes no room on disk."""

    def __init__(self, path: Path):
        super().__init__()
        self._file = open(path, "w+b")  # noqa: SIM115 (closed in close())

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def write(self, data) -> int:
        data = bytes(data)
        if len(data) >= 4096 and data.count(0) == len(data):
            self._file.seek(len(data), io.SEEK_CUR)
        else:
            self._file.write(data)
        return len(data)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def close(self) -> None:
        if not self.closed:
            self._file.close()
        super().close()


def _write_zip(path: Path, members: dict[str, bytes], *, big: int = 0) -> None:
    """A zip of `members` (deflated), after a stored `big.bin` of `big`
    zero bytes when `big` is set."""
    with _SparseWriter(path) as raw, zipfile.ZipFile(raw, "w") as zf:
        if big:
            info = zipfile.ZipInfo("big.bin")
            info.compress_type = zipfile.ZIP_STORED
            zero = bytes(MIB)
            with zf.open(info, "w") as out:
                for _ in range(big // MIB):
                    out.write(zero)
        for name, content in members.items():
            zf.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED)


class _FileRemote:
    """A RangedRemote over a local file that records each remote read:
    (kind, offset, length)."""

    def __init__(self, path: Path):
        self._file = open(path, "rb")  # noqa: SIM115 (a test double)
        self.size = os.path.getsize(path)
        self.reads: list[tuple[str, int, int]] = []

    @property
    def transferred(self) -> int:
        return sum(length for _, _, length in self.reads)

    def read_at(self, offset: int, length: int) -> bytes:
        self.reads.append(("read_at", offset, length))
        self._file.seek(offset)
        return self._file.read(length)

    def copy_range(self, offset: int, length: int, dst) -> int:
        self.reads.append(("copy_range", offset, length))
        self._file.seek(offset)
        data = self._file.read(length)
        dst.write(data)
        return len(data)


class _BytesRemote(_FileRemote):
    def __init__(self, data: bytes):
        self._file = io.BytesIO(data)
        self.size = len(data)
        self.reads = []


@pytest.fixture(scope="module")
def big_zip(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("zips") / "big.zip"
    _write_zip(path, {"app.log": APP_LOG, "other.log": b"other\n"}, big=300 * MIB)
    assert os.path.getsize(path) > 300 * MIB
    return path


# --- what an operation transfers ----------------------------------------------


def test_listing_a_300_mb_zip_transfers_less_than_2_mib(big_zip):
    remote = _FileRemote(big_zip)

    members, cut = scan_members(RangedFile(remote), "big.zip")

    assert {m.name for m in members} == {"big.bin", "app.log", "other.log"}
    assert {m.name: m.size for m in members}["big.bin"] == 300 * MIB
    assert not cut
    assert remote.transferred < 2 * MIB
    assert len(remote.reads) <= 2


def test_opening_a_member_transfers_its_compressed_size_and_at_most_2_mib_more(big_zip, tmp_path):
    remote = _FileRemote(big_zip)

    extract_member(RangedFile(remote), "big.zip", "app.log", tmp_path / "out")

    assert (tmp_path / "out").read_bytes() == APP_LOG
    compressed = zipfile.ZipFile(big_zip).getinfo("app.log").compress_size
    assert remote.transferred <= compressed + 2 * MIB
    # Nothing of the 300 MB member before it.
    assert all(offset >= 300 * MIB for _, offset, _ in remote.reads)


def test_reading_ahead_grows_to_8_mib_and_never_passes_the_member(tmp_path):
    content = os.urandom(20 * MIB)
    path = tmp_path / "random.zip"
    _write_zip(path, {"random.bin": content, "tail.log": APP_LOG})
    with zipfile.ZipFile(path) as zf:
        member, tail = zf.getinfo("random.bin"), zf.getinfo("tail.log")
    remote = _FileRemote(path)

    extract_member(RangedFile(remote), "random.zip", "random.bin", tmp_path / "out")

    assert (tmp_path / "out").read_bytes() == content
    # After the listing (the archive's end), the member's own reads.
    first = next(i for i, (_, offset, _) in enumerate(remote.reads) if offset == 0)
    inside = [(offset, length) for _, offset, length in remote.reads[first:]]
    sizes = [length for _, length in inside]
    assert sizes[:4] == [READAHEAD_START, 2 * MIB, 4 * MIB, READAHEAD_MAX]
    assert max(sizes) == READAHEAD_MAX
    assert all(offset + length <= tail.header_offset for offset, length in inside)
    assert remote.transferred <= member.compress_size + 2 * MIB


def test_a_zip64_archive_is_listed_and_read_in_place(tmp_path):
    """More entries than a classic end record holds: ZIP64 end record and
    locator, and a member with a ZIP64 local header."""
    path = tmp_path / "many.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for index in range(65_537):
            zf.writestr(f"m{index}.log", b"")
        with zf.open("last.log", "w", force_zip64=True) as out:
            out.write(APP_LOG)
    assert b"PK\x06\x06" in path.read_bytes()[-200:]

    members, _ = scan_members(RangedFile(_FileRemote(path)), "many.zip")
    extract_member(RangedFile(_FileRemote(path)), "many.zip", "last.log", tmp_path / "out")

    assert len(members) == 65_538
    assert (tmp_path / "out").read_bytes() == APP_LOG


def test_a_zip_with_data_before_it_is_read_in_place(tmp_path):
    archive = tmp_path / "plain.zip"
    _write_zip(archive, {"app.log": APP_LOG})
    path = tmp_path / "sfx.zip"
    path.write_bytes(b"MZ" + os.urandom(70_000) + archive.read_bytes())

    members, _ = scan_members(RangedFile(_FileRemote(path)), "sfx.zip")
    extract_member(RangedFile(_FileRemote(path)), "sfx.zip", "app.log", tmp_path / "out")

    assert [m.name for m in members] == ["app.log"]
    assert (tmp_path / "out").read_bytes() == APP_LOG


# --- the block cache and the budget ------------------------------------------


def test_reads_in_the_same_blocks_come_from_the_cache():
    data = os.urandom(6 * BLOCK_BYTES)
    remote = _BytesRemote(data)
    f = RangedFile(remote)

    f.seek(10)
    assert f.read(100) == data[10:110]
    f.seek(500_000)
    assert f.read(100) == data[500_000:500_100]
    assert len(remote.reads) == 1

    f.seek(BLOCK_BYTES - 50)  # across two blocks, the first cached
    assert f.read(100) == data[BLOCK_BYTES - 50 : BLOCK_BYTES + 50]
    assert remote.reads[-1] == ("read_at", BLOCK_BYTES, BLOCK_BYTES)


def test_the_least_recently_used_block_goes_first():
    remote = _BytesRemote(os.urandom(6 * BLOCK_BYTES))
    f = RangedFile(remote)
    for index in range(5):  # one more than the cache holds
        f.seek(index * BLOCK_BYTES)
        f.read(1)
    f.seek(0)
    f.read(1)
    assert len(remote.reads) == 6  # block 0 again


def test_a_read_the_budget_cant_cover_is_refused_before_it_is_made():
    remote = _BytesRemote(os.urandom(10 * BLOCK_BYTES))
    f = RangedFile(remote, limit=BLOCK_BYTES + 100)
    f.read(10)
    f.seek(5 * BLOCK_BYTES)

    with pytest.raises(FetchTooLargeError) as raised:
        f.read(BLOCK_BYTES)

    assert raised.value.limit == BLOCK_BYTES + 100
    assert remote.transferred == BLOCK_BYTES


def test_without_room_for_whole_blocks_only_what_is_asked_is_read():
    data = os.urandom(3 * BLOCK_BYTES)
    remote = _BytesRemote(data)
    f = RangedFile(remote, limit=100)
    f.seek(-22, io.SEEK_END)

    assert f.read(22) == data[-22:]
    assert remote.reads == [("read_at", len(data) - 22, 22)]


def test_a_stretch_the_budget_cant_cover_is_refused_up_front():
    remote = _BytesRemote(os.urandom(4 * BLOCK_BYTES))
    f = RangedFile(remote, limit=BLOCK_BYTES)

    with pytest.raises(FetchTooLargeError):
        f.read_sequentially(0, 2 * BLOCK_BYTES)
    assert remote.reads == []


def test_a_short_read_from_the_remote_is_a_changed_file():
    class Short(_BytesRemote):
        def read_at(self, offset, length):
            return super().read_at(offset, length)[:-1]

    with pytest.raises(RemoteFileChangedError):
        RangedFile(Short(os.urandom(1000))).read(10)


def test_the_size_checks():
    check_size(10, 10)
    check_length(5, 5)
    with pytest.raises(RemoteFileChangedError):
        check_size(10, 11)
    with pytest.raises(RemoteFileChangedError):
        check_length(5, 4)


def test_what_an_operation_read_is_logged_without_names(big_zip, monkeypatch):
    from app.collectors import ranged

    logged = []
    monkeypatch.setattr(
        ranged.logger, "debug", lambda event, **fields: logged.append((event, fields))
    )
    with ranged_file(_FileRemote(big_zip), limit=None, source_id=7, operation="list") as f:
        scan_members(f, "big.zip")

    [(event, fields)] = logged
    assert event == "archive.ranged_read"
    assert fields["source_id"] == 7 and fields["operation"] == "list"
    assert 1 <= fields["round_trips"] <= 2
    assert 0 < fields["bytes"] < 2 * MIB
    assert "seconds" in fields
    assert "app.log" not in repr(fields)


# --- SSH ---------------------------------------------------------------------

ZIP_BYTES = os.urandom(3 * MIB + 17)


def _sftp(**kwargs) -> FakeSFTPClient:
    return FakeSFTPClient(files={"/var/log/appname/logs.zip": ZIP_BYTES}, **kwargs)


def test_ssh_reads_ranges_through_one_handle(monkeypatch):
    sftp = _sftp()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    with ssh_module.open_ranged(ssh_source(), "logs.zip") as remote:
        assert remote.size == len(ZIP_BYTES)
        assert remote.read_at(5, 10) == ZIP_BYTES[5:15]
        sink = io.BytesIO()
        assert remote.copy_range(100, 2 * MIB + 3, sink) == 2 * MIB + 3
        assert sink.getvalue() == ZIP_BYTES[100 : 100 + 2 * MIB + 3]

    assert sftp.fetched_paths == ["/var/log/appname/logs.zip"]  # opened once
    assert sftp.bytes_sent == 10 + 2 * MIB + 3
    assert sftp.closed


def test_ssh_refuses_a_file_that_changed_size(monkeypatch):
    class Growing(SSHRemoteFile):
        def stat(self):
            attributes = super().stat()
            attributes.st_size += getattr(self, "grown", 0)
            return attributes

    sftp = _sftp()
    opened = []

    def open_file(path, mode="r"):
        opened.append(Growing(sftp, ZIP_BYTES))
        return opened[-1]

    monkeypatch.setattr(sftp, "open", open_file)
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    with ssh_module.open_ranged(ssh_source(), "logs.zip") as remote:
        remote.read_at(0, 10)
        opened[0].grown = 100
        with pytest.raises(RemoteFileChangedError):
            remote.read_at(0, 10)
        with pytest.raises(RemoteFileChangedError):
            remote.copy_range(0, 10, io.BytesIO())


def test_ssh_refuses_a_folder_and_a_link_the_rules_hide(monkeypatch):
    sftp = FakeSFTPClient(
        files={"/var/log/appname/hidden/secret.zip": ZIP_BYTES},
        symlinks={"/var/log/appname/logs.zip": "/var/log/appname/hidden/secret.zip"},
        dirs={"/var/log/appname/old.zip"},
    )
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    with pytest.raises(SourceNotAFileError), ssh_module.open_ranged(ssh_source(), "old.zip"):
        pass
    with (
        pytest.raises(LinkTargetHiddenError),
        ssh_module.open_ranged(
            ssh_source(), "logs.zip", target_check=lambda real, is_dir=False: False
        ),
    ):
        pass
    assert sftp.bytes_sent == 0


def test_the_connector_checks_links_with_the_rules_and_maps_roots(monkeypatch):
    sftp = FakeSFTPClient(
        files={"/srv/b/hidden/secret.zip": ZIP_BYTES, "/srv/b/logs.zip": ZIP_BYTES},
        symlinks={"/srv/b/link.zip": "/srv/b/hidden/secret.zip"},
    )
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    source = ssh_source(roots=[{"label": "a", "path": "/srv/a"}, {"label": "b", "path": "/srv/b"}])
    rules = [
        Rule(source_id=1, order=0, type=RuleType.include, pattern="**"),
        Rule(source_id=1, order=1, type=RuleType.exclude, pattern="b/hidden/**"),
    ]
    connector = CONNECTORS[Protocol.ssh]

    with connector.open_ranged(source, "b/logs.zip", rules=rules) as remote:
        assert remote.read_at(0, 4) == ZIP_BYTES[:4]
    with pytest.raises(LinkTargetHiddenError):
        with connector.open_ranged(source, "b/link.zip", rules=rules):
            pass


def test_the_connector_translates_errors_from_reads(monkeypatch):
    class Stalled(SSHRemoteFile):
        def readv(self, chunks):
            raise TimeoutError("timed out")

    sftp = _sftp()
    monkeypatch.setattr(sftp, "open", lambda path, mode="r": Stalled(sftp, ZIP_BYTES))
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

    with CONNECTORS[Protocol.ssh].open_ranged(ssh_source(), "logs.zip", rules=SHOW_ALL) as remote:
        with pytest.raises(SourceUnreachableError):
            remote.read_at(0, 10)


def test_a_changed_file_passes_through_the_connector_untranslated(monkeypatch):
    class Growing(SSHRemoteFile):
        def stat(self):
            attributes = super().stat()
            attributes.st_size += 1
            return attributes

    sftp = _sftp()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    with CONNECTORS[Protocol.ssh].open_ranged(ssh_source(), "logs.zip", rules=SHOW_ALL) as remote:
        monkeypatch.setattr(remote._remote, "_remote", Growing(sftp, ZIP_BYTES))
        with pytest.raises(RemoteFileChangedError):
            remote.read_at(0, 10)


# --- SMB ---------------------------------------------------------------------

SMB_PATH = "\\\\fileserver.example.com\\AppLogs\\logs.zip"


def test_smb_reads_ranges_through_one_handle_tolerating_the_writer(fake_smbclient):  # noqa: F811
    fake_smbclient.files[SMB_PATH] = ZIP_BYTES

    with smb_module.open_ranged(smb_source(), "logs.zip") as remote:
        assert remote.size == len(ZIP_BYTES)
        assert remote.read_at(7, 9) == ZIP_BYTES[7:16]
        sink = io.BytesIO()
        assert remote.copy_range(1, 2 * MIB, sink) == 2 * MIB
        assert sink.getvalue() == ZIP_BYTES[1 : 1 + 2 * MIB]

    [kwargs] = fake_smbclient.open_file_kwargs
    assert kwargs["share_access"] == "rwd"
    assert kwargs["buffering"] == 0
    assert fake_smbclient.reset_caches  # the connection was closed


def test_smb_refuses_a_file_that_changed_size(fake_smbclient):  # noqa: F811
    fake_smbclient.files[SMB_PATH] = ZIP_BYTES

    with smb_module.open_ranged(smb_source(), "logs.zip") as remote:
        fake_smbclient.stat_sizes[SMB_PATH] = len(ZIP_BYTES) + 1
        with pytest.raises(RemoteFileChangedError):
            remote.read_at(0, 10)
        with pytest.raises(RemoteFileChangedError):
            remote.copy_range(0, 10, io.BytesIO())


def test_smb_refuses_a_path_through_a_junction(fake_smbclient):  # noqa: F811
    fake_smbclient.files["\\\\fileserver.example.com\\AppLogs\\old\\logs.zip"] = ZIP_BYTES
    fake_smbclient.reparse_paths.add("\\\\fileserver.example.com\\AppLogs\\old")

    with pytest.raises(PathOutsideBaseError):
        with smb_module.open_ranged(smb_source(), "old/logs.zip"):
            pass
    assert fake_smbclient.open_file_kwargs == []


# --- WinRM over WinRS ---------------------------------------------------------

_ENCODED = re.compile(r"^powershell -encodedcommand (?P<script>[A-Za-z0-9+/=]+)$")


class _WinRSHost:
    """Runs the ranged-read scripts against `content` as PowerShell would,
    and records the sessions, shells and commands they took."""

    def __init__(self, content: bytes):
        self.content = content
        self.size_now: int | None = None
        self.is_dir = False
        self.sessions: list = []
        self.scripts: list[str] = []
        self.refuse_envelope = False

    @property
    def current(self) -> int:
        return len(self.content) if self.size_now is None else self.size_now

    def run(self, script: str) -> tuple[bytes, int]:
        self.scripts.append(script)
        if self.is_dir and "[IO.Directory]::Exists" in script:
            return b"PERCHTAIL_IS_A_DIRECTORY\r\n", 6
        if ").Length" in script and "Get-Item" in script:
            return f"{len(self.content)}\r\n".encode(), 0
        at = re.search(r"New-Object byte\[\] (\d+); \$n = 0; if \((\d+) -lt \$len\)", script)
        if at:
            length, offset = int(at.group(1)), int(at.group(2))
            data = self.content[offset : offset + length]
            line = f"{len(self.content)}|{self.current}|{base64.b64encode(data).decode()}"
            return (line + "\r\n").encode(), 0
        ranged = re.search(
            r"\$size = \[long\](\d+);.*\$f\.Seek\(\[long\](\d+), 'Begin'\)"
            r".*\$left = \[long\](\d+);",
            script,
        )
        if ranged:
            size, offset, length = (int(g) for g in ranged.groups())
            if self.current != size:
                return b"PERCHTAIL_CHANGED\r\n", 7
            data = self.content[offset : offset + length]
            lines = [
                base64.b64encode(data[i : i + 3 * MIB]).decode()
                for i in range(0, len(data), 3 * MIB)
            ]
            return ("\r\n".join([*lines, f"PERCHTAIL_END {len(data)}"]) + "\r\n").encode(), 0
        raise AssertionError(script)


class _FakeTransport:
    def __init__(self, session):
        self._session = session

    def close_session(self):
        self._session.closed = True


class _FakeProtocol:
    def __init__(self, host: _WinRSHost, session):
        self._host = host
        self._session = session
        self._commands: dict[str, tuple[bytes, int]] = {}
        self.transport = _FakeTransport(session)

    def open_shell(self):
        if self._host.refuse_envelope and self._session.envelope_size != 153600:
            raise _fault()
        self._session.shells_opened += 1
        return f"shell-{self._session.shells_opened}"

    def run_command(self, shell_id, command):
        script = base64.b64decode(_ENCODED.match(command)["script"]).decode("utf_16_le")
        command_id = f"command-{len(self._commands)}"
        self._commands[command_id] = self._host.run(script)
        return command_id

    def get_command_output_raw(self, shell_id, command_id):
        out, code = self._commands[command_id]
        return out, b"", code, True

    def cleanup_command(self, shell_id, command_id):
        pass

    def close_shell(self, shell_id, close_session=True):
        assert close_session is False
        self._session.shells_closed += 1


class _FakeSession:
    def __init__(self, host: _WinRSHost, envelope_size: int):
        self.envelope_size = envelope_size
        self.shells_opened = 0
        self.shells_closed = 0
        self.closed = False
        self.protocol = _FakeProtocol(host, self)

    def _clean_error_msg(self, message: bytes) -> bytes:
        return message


@pytest.fixture()
def winrs(monkeypatch) -> _WinRSHost:
    host = _WinRSHost(os.urandom(7 * MIB + 5))

    def session(source, envelope_size):
        host.sessions.append(_FakeSession(host, envelope_size))
        return host.sessions[-1]

    monkeypatch.setattr(winrm_module, "_session", session)
    monkeypatch.setattr(winrm_module, "_run_ps", lambda *a, **k: pytest.fail("run_ps used"))
    return host


def _winrm_source(**overrides) -> Source:
    defaults = dict(
        name="win01",
        protocol=Protocol.winrm,
        host="win01.example.com",
        base_path="D:\\Logs",
        credential_ref="encrypted",
        winrm_scheme="https",
    )
    defaults.update(overrides)
    return Source(**defaults)


def test_winrs_reads_ranges_in_one_session_and_one_shell(winrs):
    with winrm_module.open_ranged(_winrm_source(), "old/logs.zip") as remote:
        assert remote.size == len(winrs.content)
        assert remote.read_at(11, 1000) == winrs.content[11:1011]
        sink = io.BytesIO()
        assert remote.copy_range(3, 7 * MIB, sink) == 7 * MIB
        assert sink.getvalue() == winrs.content[3 : 3 + 7 * MIB]

    [session] = winrs.sessions
    assert (session.shells_opened, session.shells_closed, session.closed) == (1, 1, True)
    assert len(winrs.scripts) == 3
    for script in winrs.scripts:
        # Every read checks the path again before opening the file.
        guard = script.index("ReparsePoint")
        assert guard < script.index("[IO.File]::Exists")
        assert "D:\\Logs\\old\\logs.zip" in script


def test_winrs_refuses_a_file_that_changed_size(winrs):
    with winrm_module.open_ranged(_winrm_source(), "logs.zip") as remote:
        winrs.size_now = len(winrs.content) + 1
        with pytest.raises(RemoteFileChangedError):
            remote.read_at(0, 10)
        with pytest.raises(RemoteFileChangedError):
            remote.copy_range(0, 10, io.BytesIO())


def test_winrs_refuses_a_folder(winrs):
    winrs.is_dir = True
    with pytest.raises(SourceNotAFileError):
        with winrm_module.open_ranged(_winrm_source(), "logs.zip"):
            pass
    [session] = winrs.sessions
    assert session.closed


def test_winrs_falls_back_to_the_smaller_envelope_once(winrs):
    winrs.refuse_envelope = True
    with winrm_module.open_ranged(_winrm_source(), "logs.zip") as remote:
        assert remote.read_at(0, 4) == winrs.content[:4]
    assert [s.envelope_size for s in winrs.sessions] == [512000, 153600]
    assert all(s.closed for s in winrs.sessions)


# --- WinRM behind JEA ----------------------------------------------------------


def test_jea_reads_ranges_on_one_runspace(endpoint):  # noqa: F811
    with winrm_module.open_ranged(jea_source(), "app.log") as remote:
        assert remote.size == len(JEA_FILE)
        assert remote.read_at(5, 4) == JEA_FILE[5:9]
        sink = io.BytesIO()
        assert remote.copy_range(0, len(JEA_FILE), sink) == len(JEA_FILE)
        assert sink.getvalue() == JEA_FILE

    assert len(endpoint.pools) == 1
    commands = [command for command, _ in endpoint.calls]
    assert commands == [
        "Get-PerchTailSize",
        "Read-PerchTailRange",
        "Read-PerchTailBlock",
        "Get-PerchTailSize",
    ]
    assert all(params["Path"] == "D:\\Logs\\app.log" for _, params in endpoint.calls)


def test_jea_refuses_a_file_that_changed_size(endpoint, monkeypatch):  # noqa: F811
    answer = endpoint.answer

    def grown(command, params):
        if command == "Get-PerchTailSize" and len(endpoint.calls) > 1:
            return str(len(JEA_FILE) + 1)
        if command == "Read-PerchTailRange" and len(endpoint.calls) > 1:
            return answer(command, params).replace(str(len(JEA_FILE)), str(len(JEA_FILE) + 1), 1)
        return answer(command, params)

    monkeypatch.setattr(endpoint, "answer", grown)
    with winrm_module.open_ranged(jea_source(), "app.log") as remote:
        with pytest.raises(RemoteFileChangedError):
            remote.read_at(0, 4)
        with pytest.raises(RemoteFileChangedError):
            remote.copy_range(0, 4, io.BytesIO())
