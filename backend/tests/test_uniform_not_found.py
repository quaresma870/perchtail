"""Every read of a path that can't be read gets the same 404 as a path that
isn't on the source: hidden by the rules, a link to a hidden target or out
of the base path, a folder asked for as a file, an archive member that is
hidden or missing. Status, body and headers are compared whole (but for
the headers that differ on every response), on browse, open, follow,
download and download-zip, over the connectors' fakes."""

import io
import os
import zipfile
from types import SimpleNamespace

import app.collectors.smb as smb_module
import app.collectors.ssh as ssh_module
import pytest
from app.agent_registry import AgentCommandError, get_agent_registry
from app.models import Protocol, Rule, RuleType
from smbprotocol.exceptions import SMBOSError

from tests.test_collectors_smb import FakeSMBClient
from tests.test_collectors_ssh import FakeAttr, FakeSFTPClient, FakeSSHClient
from tests.test_collectors_winrm import (
    FakeResult,
    _read_output,
    _responses,
    fake_session,  # noqa: F401 - a fixture
)
from tests.test_link_targets import RULES, _audit, _rule, api, tree  # noqa: F401
from tests.test_not_a_file_agent import FolderAgent
from tests.test_redaction_endpoints import _enable_redaction

# Different on every response.
_VARYING_HEADERS = {"x-request-id", "date"}


def _comparable(response) -> tuple:
    headers = {k: v for k, v in response.headers.items() if k.lower() not in _VARYING_HEADERS}
    return response.status_code, response.content, headers


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buffer.getvalue()


_ZIP = _zip_bytes({"ok.log": b"ok\n", "hidden.log": b"h\n", "sub/in.log": b"in\n"})
# Shown by RULES and hidden by this one.
_HIDE_MEMBER = (3, RuleType.exclude, "**/hidden.log")

# (endpoint, extra query parameters); download-zip and browse ask for
# folders, the others for files.
_FILE_ENDPOINTS = [("open", {}), ("follow", {"offset": 0}), ("download", {})]
_FOLDER_ENDPOINTS = [("browse", {}), ("download-zip", {})]
_MISSING_FILE = {"path": "public/missing.log"}
_MISSING_FOLDER = {"path": "public/missing"}


def _assert_same_as_missing(client, source, endpoint, extra, cases, missing):
    url = f"/sources/{source.id}/{endpoint}"
    expected = client.get(url, params={**missing, **extra})
    assert expected.status_code == 404, expected.text
    different = {}
    for name, params in cases.items():
        response = client.get(url, params={**params, **extra})
        if _comparable(response) != _comparable(expected):
            different[name] = (response.status_code, response.text)
    assert not different, f"{endpoint}: expected {expected.text}, got {different!r}"


def _add_rule(session, source, order, type_, pattern) -> None:
    session.add(Rule(source_id=source.id, order=order, type=type_, pattern=pattern))
    session.commit()


# --- local ---------------------------------------------------------------------

_LOCAL_FILES = {
    "hidden by the rules": {"path": "secret/key.log"},
    "hidden folder's file": {"path": "archive/2026/old.log"},
    "link to a hidden file": {"path": "sneaky.log"},
    "link out of the base path": {"path": "outside.log"},
    "link in a folder to a hidden file": {"path": "public/inner.log"},
    "link to a hidden folder": {"path": "aponta"},
    "folder": {"path": "public"},
    "link to a folder": {"path": "mirror"},
}
_LOCAL_MEMBERS = {
    "missing member": {"path": "public/logs.zip", "member": "nope.log"},
    "hidden member": {"path": "public/logs.zip", "member": "hidden.log"},
    "member of a hidden archive": {"path": "secret/s.zip", "member": "ok.log"},
    "member of a missing archive": {"path": "public/missing.zip", "member": "ok.log"},
}
_LOCAL_FOLDERS = {
    "hidden by the rules": {"path": "secret"},
    "hidden folder's folder": {"path": "archive/2026"},
    "link to a hidden folder": {"path": "logs"},
    "other link to a hidden folder": {"path": "aponta"},
    "link out of the base path": {"path": "outdir"},
}
_LOCAL_ARCHIVE_LISTINGS = {
    "hidden archive": {"path": "secret/s.zip"},
    "missing archive": {"path": "public/missing.zip"},
    "hidden folder in an archive": {"path": "public/logs.zip/hid"},
    "missing folder in an archive": {"path": "public/logs.zip/nope"},
    "file in an archive asked for as a folder": {"path": "public/logs.zip/ok.log"},
    "hidden file in an archive asked for as a folder": {"path": "public/logs.zip/hidden.log"},
}


@pytest.fixture()
def local(api, session, tree, tmp_path):  # noqa: F811
    """`api` (tests/test_link_targets.py) with: aponta -> secret, outdir ->
    a folder out of the base path, public/logs.zip and secret/s.zip, and a
    rule hiding hidden.log and the folder hid/ wherever they are."""
    client, source = api
    (tree / "aponta").symlink_to("secret", target_is_directory=True)
    (tree / "outdir").symlink_to(tmp_path / "outside", target_is_directory=True)
    zipped = _zip_bytes(
        {"ok.log": b"ok\n", "hidden.log": b"h\n", "sub/in.log": b"in\n", "hid/x.log": b"x\n"}
    )
    (tree / "public" / "logs.zip").write_bytes(zipped)
    (tree / "secret" / "s.zip").write_bytes(zipped)
    _add_rule(session, source, *_HIDE_MEMBER)
    _add_rule(session, source, 4, RuleType.exclude, "**/hid/**")
    return client, source


def _redaction(session, redacted: bool) -> None:
    if redacted:
        _enable_redaction(session)


@pytest.mark.parametrize("redacted", [False, True], ids=["plain", "redacted"])
@pytest.mark.parametrize(("endpoint", "extra"), _FILE_ENDPOINTS)
def test_local_file_reads_answer_as_a_missing_file(local, session, redacted, endpoint, extra):
    _redaction(session, redacted)
    client, source = local
    cases = dict(_LOCAL_FILES)
    if endpoint != "follow":
        cases.update(_LOCAL_MEMBERS)
    _assert_same_as_missing(client, source, endpoint, extra, cases, _MISSING_FILE)


@pytest.mark.parametrize("redacted", [False, True], ids=["plain", "redacted"])
@pytest.mark.parametrize(("endpoint", "extra"), _FOLDER_ENDPOINTS)
def test_local_folder_reads_answer_as_a_missing_folder(local, session, redacted, endpoint, extra):
    _redaction(session, redacted)
    client, source = local
    cases = dict(_LOCAL_FOLDERS)
    if endpoint == "browse":
        cases.update(_LOCAL_ARCHIVE_LISTINGS)
    _assert_same_as_missing(client, source, endpoint, extra, cases, _MISSING_FOLDER)


def test_local_answer_is_the_missing_path_body(local):
    """The one body every case gets: today's answer for a path that isn't
    there, nothing added."""
    client, source = local
    for endpoint, extra in _FILE_ENDPOINTS:
        response = client.get(
            f"/sources/{source.id}/{endpoint}", params={"path": "sneaky.log", **extra}
        )
        assert response.status_code == 404
        assert response.json() == {
            "detail": "Not found on the source",
            "error_code": "not_found_on_source",
        }


@pytest.mark.parametrize(
    ("endpoint", "extra", "path", "action", "reason"),
    [
        ("open", {}, "sneaky.log", "file.open_denied", "link_target_hidden"),
        ("follow", {"offset": 0}, "public", "file.open_denied", "not_a_file"),
        ("download", {}, "outside.log", "file.download_denied", "link_target_hidden"),
    ],
)
def test_local_refusals_keep_their_audit(local, session, endpoint, extra, path, action, reason):
    client, source = local
    client.get(f"/sources/{source.id}/{endpoint}", params={"path": path, **extra})
    [entry] = _audit(session, action)
    assert entry.event_metadata["reason"] == reason
    assert _audit(session, "access.denied") == []


def test_local_browse_of_a_hidden_link_keeps_its_audit(local, session):
    client, source = local
    client.get(f"/sources/{source.id}/browse", params={"path": "logs"})
    [entry] = _audit(session, "access.denied")
    assert entry.event_metadata["reason"] == "link_target_hidden"


def test_local_folders_in_an_archive_are_still_listed(local):
    """A folder of the archive is listed, whether the archive names it or
    only the members below it do; one whose members the rules all hide is
    listed empty, as such a folder on the source."""
    client, source = local
    url = f"/sources/{source.id}/browse"
    names = [e["name"] for e in client.get(url, params={"path": "public/logs.zip/sub"}).json()]
    assert names == ["in.log"]
    root = {e["name"] for e in client.get(url, params={"path": "public/logs.zip"}).json()}
    assert {"ok.log", "sub"} <= root and not root & {"hidden.log", "hid"}


def test_local_rule_hidden_paths_are_still_not_audited(local, session):
    client, source = local
    client.get(f"/sources/{source.id}/open", params={"path": "secret/key.log"})
    client.get(f"/sources/{source.id}/browse", params={"path": "secret"})
    assert _audit(session, "access.denied") == []
    assert _audit(session, "file.open_denied") == []


# --- SSH -----------------------------------------------------------------------

_SSH_BASE = "/var/log/appname"


class _SFTPTree(FakeSFTPClient):
    """FakeSFTPClient whose listing is per folder and fails on a folder that
    isn't there."""

    def listdir_attr(self, path):
        self.requested_paths.append(path)
        if path not in self._dirs:
            raise FileNotFoundError(path)
        prefix = path.rstrip("/") + "/"
        names = {
            p[len(prefix) :].split("/", 1)[0]
            for p in (*self._files, *self._dirs, *self._symlinks)
            if p.startswith(prefix) and p != path
        }
        entries = []
        for name in sorted(names):
            full = prefix + name
            if full in self._dirs:
                entries.append(FakeAttr(name, True))
            elif full in self._files:
                entries.append(FakeAttr(name, False, len(self._files[full])))
        return entries


def _ssh_tree() -> _SFTPTree:
    b = _SSH_BASE
    return _SFTPTree(
        files={
            f"{b}/public/p.log": b"public\n",
            f"{b}/public/logs.zip": _ZIP,
            f"{b}/secret/key.log": b"TOP SECRET\n",
            f"{b}/secret/s.zip": _ZIP,
        },
        symlinks={
            f"{b}/sneaky.log": f"{b}/secret/key.log",
            f"{b}/outside.log": "/etc/passwd",
            f"{b}/dangling.log": f"{b}/secret/missing.log",
            f"{b}/mirror": f"{b}/public",
            f"{b}/aponta": f"{b}/secret",
            f"{b}/outdir": "/etc",
        },
        dirs={b, f"{b}/public", f"{b}/secret", "/etc"},
    )


@pytest.fixture()
def ssh(local, session, monkeypatch):
    client, source = local
    source.protocol = Protocol.ssh
    source.base_path = _SSH_BASE
    source.credential_ref = "encrypted-blob"
    session.add(source)
    session.commit()
    sftp = _ssh_tree()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    return client, source


_REMOTE_FILES = {
    "hidden by the rules": {"path": "secret/key.log"},
    "link to a hidden file": {"path": "sneaky.log"},
    "link out of the base path": {"path": "outside.log"},
    "link to a hidden path that isn't there": {"path": "dangling.log"},
    "link to a hidden folder": {"path": "aponta"},
    "folder": {"path": "public"},
    "link to a folder": {"path": "mirror"},
}
_REMOTE_MEMBERS = {
    "missing member": {"path": "public/logs.zip", "member": "nope.log"},
    "hidden member": {"path": "public/logs.zip", "member": "hidden.log"},
    "member of a hidden archive": {"path": "secret/s.zip", "member": "ok.log"},
    "member of a missing archive": {"path": "public/missing.zip", "member": "ok.log"},
}
_REMOTE_FOLDERS = {
    "hidden by the rules": {"path": "secret"},
    "link to a hidden folder": {"path": "aponta"},
    "link out of the base path": {"path": "outdir"},
}
_REMOTE_ARCHIVE_LISTINGS = {
    "hidden archive": {"path": "secret/s.zip"},
    "missing archive": {"path": "public/missing.zip"},
    "missing folder in an archive": {"path": "public/logs.zip/nope"},
    "file in an archive asked for as a folder": {"path": "public/logs.zip/ok.log"},
}


@pytest.mark.parametrize("redacted", [False, True], ids=["plain", "redacted"])
@pytest.mark.parametrize(("endpoint", "extra"), _FILE_ENDPOINTS)
def test_ssh_file_reads_answer_as_a_missing_file(ssh, session, redacted, endpoint, extra):
    _redaction(session, redacted)
    client, source = ssh
    cases = dict(_REMOTE_FILES)
    if endpoint != "follow":
        cases.update(_REMOTE_MEMBERS)
    _assert_same_as_missing(client, source, endpoint, extra, cases, _MISSING_FILE)


@pytest.mark.parametrize(("endpoint", "extra"), _FOLDER_ENDPOINTS)
def test_ssh_folder_reads_answer_as_a_missing_folder(ssh, endpoint, extra):
    client, source = ssh
    cases = dict(_REMOTE_FOLDERS)
    if endpoint == "browse":
        cases.update(_REMOTE_ARCHIVE_LISTINGS)
    _assert_same_as_missing(client, source, endpoint, extra, cases, _MISSING_FOLDER)


# --- SMB -----------------------------------------------------------------------

_SMB_ROOT = "\\\\fileserver.example.com\\AppLogs"
_DIRECTORY = 0x10
_REPARSE = 0x400
_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034


class _SMBTree(FakeSMBClient):
    """FakeSMBClient with folders, a junction (a reparse point, never
    followed) and NT status codes for what isn't there."""

    def __init__(self):
        super().__init__()
        r = _SMB_ROOT
        self.files = {f"{r}\\public\\p.log": b"public\n", f"{r}\\public\\logs.zip": _ZIP}
        self.folders = {r, f"{r}\\public", f"{r}\\secret"}
        self.reparse_paths = {f"{r}\\junction"}

    def _missing(self, path):
        return SMBOSError(_STATUS_OBJECT_NAME_NOT_FOUND, path)

    def lstat(self, path, **kwargs):
        if path in self.reparse_paths:
            return SimpleNamespace(st_file_attributes=_REPARSE | _DIRECTORY, st_size=0)
        if path in self.folders:
            return SimpleNamespace(st_file_attributes=_DIRECTORY, st_size=0)
        if path in self.files:
            return SimpleNamespace(st_file_attributes=0, st_size=len(self.files[path]))
        raise self._missing(path)

    def stat(self, path, **kwargs):
        self.stat_kwargs.append(kwargs)
        return self.lstat(path)

    def scandir(self, path, **kwargs):
        self.requested_paths.append(path)
        if path not in self.folders:
            raise self._missing(path)
        return []

    def open_file(self, path, mode="rb", **kwargs):
        self.requested_paths.append(path)
        self.open_file_kwargs.append(kwargs)
        if path not in self.files:
            raise self._missing(path)
        return io.BytesIO(self.files[path])


@pytest.fixture()
def smb(local, session, monkeypatch):
    client, source = local
    source.protocol = Protocol.smb
    source.host = "fileserver.example.com"
    source.base_path = "AppLogs"
    source.credential_ref = "encrypted"
    session.add(source)
    session.commit()
    monkeypatch.setattr(smb_module, "smbclient", _SMBTree())
    monkeypatch.setattr(
        smb_module, "decrypt_credential", lambda ref: {"username": "svc", "password": "s3cret"}
    )
    return client, source


_SMB_FILES = {
    "hidden by the rules": {"path": "secret/key.log"},
    "junction": {"path": "junction/a.log"},
    "folder": {"path": "public"},
    **_REMOTE_MEMBERS,
}


@pytest.mark.parametrize(("endpoint", "extra"), _FILE_ENDPOINTS)
def test_smb_file_reads_answer_as_a_missing_file(smb, endpoint, extra):
    client, source = smb
    cases = {k: v for k, v in _SMB_FILES.items() if endpoint != "follow" or "member" not in v}
    _assert_same_as_missing(client, source, endpoint, extra, cases, _MISSING_FILE)


@pytest.mark.parametrize(("endpoint", "extra"), _FOLDER_ENDPOINTS)
def test_smb_folder_reads_answer_as_a_missing_folder(smb, endpoint, extra):
    client, source = smb
    cases = {"hidden by the rules": {"path": "secret"}, "junction": {"path": "junction"}}
    _assert_same_as_missing(client, source, endpoint, extra, cases, _MISSING_FOLDER)


# --- WinRM ---------------------------------------------------------------------


def _winrm_answer(script: str):
    """What Windows answers, by the path the script asks for: a junction
    out of the base path, a folder, a file that isn't there."""
    if "junction" in script:
        return FakeResult(status_code=4, std_out=b"PERCHTAIL_OUTSIDE_BASE\r\n")
    if "missing" in script:
        return FakeResult(status_code=5, std_out=b"PERCHTAIL_NOT_FOUND\r\n")
    if "AppName\\public'" in script:
        return FakeResult(status_code=6, std_out=b"PERCHTAIL_IS_A_DIRECTORY\r\n")
    return None


@pytest.fixture()
def winrm(local, session, fake_session):  # noqa: F811
    client, source = local
    source.protocol = Protocol.winrm
    source.host = "win01.example.com"
    source.base_path = "C:\\Logs\\AppName"
    source.credential_ref = "encrypted"
    session.add(source)
    session.commit()
    content = b"public\n"

    def handler(script):
        answer = _winrm_answer(script)
        if answer is not None:
            return answer
        return FakeResult(std_out=str(len(content)).encode())

    def stream(script):
        answer = _winrm_answer(script)
        if answer is not None:
            return _responses(answer.std_out, 0, answer.status_code)
        return _responses(_read_output(content, script))

    fake_session.handler = handler
    fake_session.stream_handler = stream
    return client, source


_WINRM_FILES = {
    "hidden by the rules": {"path": "secret/key.log"},
    "junction": {"path": "junction/a.log"},
    "folder": {"path": "public"},
}


@pytest.mark.parametrize(("endpoint", "extra"), _FILE_ENDPOINTS)
def test_winrm_file_reads_answer_as_a_missing_file(winrm, endpoint, extra):
    client, source = winrm
    _assert_same_as_missing(client, source, endpoint, extra, _WINRM_FILES, _MISSING_FILE)


@pytest.mark.parametrize(("endpoint", "extra"), _FOLDER_ENDPOINTS)
def test_winrm_folder_reads_answer_as_a_missing_folder(winrm, endpoint, extra):
    client, source = winrm
    cases = {"hidden by the rules": {"path": "secret"}, "junction": {"path": "junction"}}
    _assert_same_as_missing(client, source, endpoint, extra, cases, _MISSING_FOLDER)


# --- push agent ----------------------------------------------------------------


class _TreeAgent(FolderAgent):
    """FolderAgent that also says a folder to list isn't there."""

    def __call__(self, source_id, command_type, timeout=30.0, **fields):
        path = fields.get("path", "")
        if command_type == "list" and not os.path.exists(os.path.join(self.base, path)):
            raise AgentCommandError(
                f"open {path}: no such file or directory", message_type="list_error"
            )
        return super().__call__(source_id, command_type, timeout, **fields)


@pytest.fixture()
def agent(local, session, tree, monkeypatch):  # noqa: F811
    client, source = local
    source.protocol = Protocol.agent
    source.base_path = "/var/log"
    session.add(source)
    session.commit()
    fake = _TreeAgent(tree)
    registry = get_agent_registry()
    monkeypatch.setattr(registry, "send_command_sync", fake)
    monkeypatch.setattr(registry, "is_connected", lambda source_id: True)
    monkeypatch.setattr(registry, "features", lambda source_id: fake.features)
    return client, source


@pytest.mark.parametrize(("endpoint", "extra"), _FILE_ENDPOINTS)
def test_agent_file_reads_answer_as_a_missing_file(agent, endpoint, extra):
    client, source = agent
    _assert_same_as_missing(client, source, endpoint, extra, _LOCAL_FILES, _MISSING_FILE)


@pytest.mark.parametrize(("endpoint", "extra"), _FOLDER_ENDPOINTS)
def test_agent_folder_reads_answer_as_a_missing_folder(agent, endpoint, extra):
    client, source = agent
    cases = {k: v for k, v in _LOCAL_FOLDERS.items() if k != "link out of the base path"}
    _assert_same_as_missing(client, source, endpoint, extra, cases, _MISSING_FOLDER)


# Silences "imported but unused" for the rules the fixtures above build on.
assert RULES and _rule
