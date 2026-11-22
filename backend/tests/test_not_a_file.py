"""A folder (or a link to one) asked for as a file -- open, follow, download
-- is answered exactly as a path that isn't there (404, the same body), on
every connector, and audited with the reason `not_a_file`. A link to a
folder the rules hide is checked on that folder, as browse does, and is
refused as `link_target_hidden` (P1 of the 0.7.0 preview retest)."""

import errno

import anyio
import app.api.archive as archive_module
import app.collectors.smb as smb_module
import app.collectors.ssh as ssh_module
import app.collectors.winrm as winrm_module
import pytest
from app.collectors import local as local_module
from app.collectors.base import (
    LinkTargetHiddenError,
    SourceNotAFileError,
    SourceNotFoundError,
)
from app.collectors.errors import translate_local, translate_smb
from app.collectors.registry import CONNECTORS
from app.models import Protocol, RuleType, Source

from tests.test_collectors_smb import FakeSMBClient
from tests.test_collectors_smb import _source as smb_source
from tests.test_collectors_ssh import FakeSFTPClient, FakeSSHClient
from tests.test_collectors_ssh import _source as ssh_source
from tests.test_collectors_winrm import (
    FakeResult,
    _responses,
    _serve_file,
    fake_session,  # noqa: F401 - a fixture
)
from tests.test_collectors_winrm import _source as winrm_source
from tests.test_link_targets import RULES, _audit, _rule, _source, api, tree  # noqa: F401
from tests.test_redaction_endpoints import _enable_redaction

# --- the shared refusal --------------------------------------------------------


def test_not_a_file_answers_as_a_missing_path():
    refused, missing = SourceNotAFileError("x"), SourceNotFoundError("x")
    assert refused.status_code == missing.status_code == 404
    assert refused.public_message == missing.public_message
    assert refused.error_code == missing.error_code


def test_a_folder_opened_as_a_file_is_translated_to_not_a_file():
    assert isinstance(translate_local(IsADirectoryError("x")), SourceNotAFileError)
    assert isinstance(translate_local(OSError(errno.EISDIR, "x")), SourceNotAFileError)
    # The other errors don't change.
    assert type(translate_local(FileNotFoundError("x"))) is SourceNotFoundError
    assert type(translate_local(NotADirectoryError("x"))) is SourceNotFoundError


# --- local -------------------------------------------------------------------


@pytest.fixture()
def hidden_folder_link(tree):  # noqa: F811
    """aponta -> secret: a link to a folder `exclude secret/**` hides. The
    rule doesn't match the folder `secret` itself, only what is in it."""
    (tree / "aponta").symlink_to("secret", target_is_directory=True)
    return tree


@pytest.mark.parametrize("path", ["public", "mirror", "archive/2026"])
def test_local_reads_of_a_folder_are_not_a_file(hidden_folder_link, tmp_path, path):
    source = _source(hidden_folder_link)
    connector = CONNECTORS[Protocol.local]
    with pytest.raises(SourceNotAFileError):
        connector.read_range(source, path, 0, 100, rules=RULES)
    with pytest.raises(SourceNotAFileError):
        connector.fetch_file(source, path, tmp_path / "out", rules=RULES)
    with pytest.raises(SourceNotAFileError):
        with connector.local_copy(source, path, rules=RULES):
            pass
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("path", ["aponta", "logs"])
def test_local_reads_of_a_link_to_a_hidden_folder_are_link_target_hidden(
    hidden_folder_link, tmp_path, path
):
    source = _source(hidden_folder_link)
    connector = CONNECTORS[Protocol.local]
    with pytest.raises(LinkTargetHiddenError):
        connector.read_range(source, path, 0, 100, rules=RULES)
    with pytest.raises(LinkTargetHiddenError):
        connector.fetch_file(source, path, tmp_path / "out", rules=RULES)


def test_local_link_to_a_folder_without_rules_is_not_followed(tree):  # noqa: F811
    connector = CONNECTORS[Protocol.local]
    with pytest.raises(LinkTargetHiddenError):
        connector.read_range(_source(tree), "mirror", 0, 100)
    # A folder not reached through a link needs no rules to be refused.
    with pytest.raises(SourceNotAFileError):
        connector.read_range(_source(tree), "public", 0, 100)


def test_local_folders_of_a_source_with_several_roots(hidden_folder_link, tmp_path):
    other = tmp_path / "allowed" / "other"
    other.mkdir()
    source = Source(
        id=1,
        name="s",
        protocol=Protocol.local,
        host="h",
        base_path=str(hidden_folder_link),
        roots=[
            {"label": "one", "path": str(hidden_folder_link)},
            {"label": "two", "path": str(other)},
        ],
    )
    rules = [
        _rule(0, RuleType.include, "**/*"),
        _rule(1, RuleType.exclude, "one/secret/**"),
        _rule(2, RuleType.exclude, "one/archive/**"),
    ]
    connector = CONNECTORS[Protocol.local]
    for path in ("one", "two", "one/public", "one/mirror"):
        with pytest.raises(SourceNotAFileError):
            connector.read_range(source, path, 0, 100, rules=rules)
    for path in ("one/aponta", "one/logs"):
        with pytest.raises(LinkTargetHiddenError):
            connector.read_range(source, path, 0, 100, rules=rules)


def test_local_files_and_links_to_files_still_read(tree):  # noqa: F811
    connector = CONNECTORS[Protocol.local]
    assert connector.read_range(_source(tree), "current.log", 0, 100, rules=RULES).data == (
        b"today\n"
    )
    assert local_module.resolve_file(_source(tree), "app-2026.log").name == "app-2026.log"


# --- local: the endpoints ------------------------------------------------------

_ENDPOINTS = [
    ("open", {}, "file.open_denied", {}),
    ("follow", {"offset": 0}, "file.open_denied", {"follow": True}),
    ("download", {}, "file.download_denied", {}),
]
# Different on every response.
_VARYING_HEADERS = {"x-request-id", "date"}


def _comparable(response) -> tuple:
    headers = {k: v for k, v in response.headers.items() if k.lower() not in _VARYING_HEADERS}
    return response.status_code, response.content, headers


@pytest.mark.parametrize(("endpoint", "params", "action", "extra"), _ENDPOINTS)
@pytest.mark.parametrize("path", ["public", "mirror"])
def test_a_folder_read_from_an_endpoint_is_an_audited_404(
    api, session, hidden_folder_link, endpoint, params, action, extra, path  # noqa: F811
):
    client, source = api
    response = client.get(f"/sources/{source.id}/{endpoint}", params={"path": path, **params})
    assert response.status_code == 404
    [entry] = _audit(session, action)
    assert entry.event_metadata["path"] == path
    assert entry.event_metadata["reason"] == "not_a_file"
    for name, value in extra.items():
        assert entry.event_metadata[name] == value
    # Recorded once, not again as access.denied.
    assert _audit(session, "access.denied") == []


@pytest.mark.parametrize(("endpoint", "params", "action", "extra"), _ENDPOINTS)
@pytest.mark.parametrize("path", ["aponta", "logs"])
def test_a_link_to_a_hidden_folder_read_from_an_endpoint_is_link_target_hidden(
    api, session, hidden_folder_link, endpoint, params, action, extra, path  # noqa: F811
):
    client, source = api
    response = client.get(f"/sources/{source.id}/{endpoint}", params={"path": path, **params})
    assert response.status_code == 404
    [entry] = _audit(session, action)
    assert entry.event_metadata["path"] == path
    assert entry.event_metadata["reason"] == "link_target_hidden"
    # Browse of the same link answers the same way.
    response = client.get(f"/sources/{source.id}/browse", params={"path": path})
    assert response.status_code == 404


@pytest.mark.parametrize(("endpoint", "params", "action", "extra"), _ENDPOINTS)
def test_a_folder_answers_exactly_as_a_missing_path(
    api, hidden_folder_link, endpoint, params, action, extra  # noqa: F811
):
    """The answer about a folder can't be told from the answer about a file
    that isn't there."""
    client, source = api
    url = f"/sources/{source.id}/{endpoint}"
    missing = client.get(url, params={"path": "public/missing.log", **params})
    assert missing.status_code == 404
    for path in ("public", "mirror"):
        folder = client.get(url, params={"path": path, **params})
        assert _comparable(folder) == _comparable(missing)


# --- local: a file that isn't there --------------------------------------------


def _not_found_body() -> dict:
    error = SourceNotFoundError("x")
    body: dict = {"detail": error.public_message}
    if error.error_code:
        body["error_code"] = error.error_code
    return body


@pytest.mark.parametrize("redacted", [False, True], ids=["plain", "redacted"])
@pytest.mark.parametrize(("endpoint", "params", "action", "extra"), _ENDPOINTS)
def test_a_missing_local_file_is_not_found(
    api, session, tree, redacted, endpoint, params, action, extra  # noqa: F811
):
    """A local file that isn't there answers as on every other connector
    (SourceNotFoundError, 404), served in place or redacted, never 500."""
    if redacted:
        _enable_redaction(session)
    client, source = api
    response = client.get(
        f"/sources/{source.id}/{endpoint}", params={"path": "public/missing.log", **params}
    )
    assert response.status_code == 404
    assert response.json() == _not_found_body()
    # Nothing was read, so nothing is audited as read.
    assert _audit(session, "file.open") == []
    assert _audit(session, "file.download") == []


@pytest.mark.parametrize("redacted", [False, True], ids=["plain", "redacted"])
@pytest.mark.parametrize("endpoint", ["open", "download"])
def test_a_local_file_gone_before_it_is_read_is_not_found(
    api, session, tree, monkeypatch, redacted, endpoint  # noqa: F811
):
    """A local file gone between the check and the read (a log rotated away
    at that moment) answers the same 404, not 500."""
    if redacted:
        _enable_redaction(session)
    local_plain_path = archive_module._local_plain_path

    def then_gone(source, path, rules):
        resolved = local_plain_path(source, path, rules)
        resolved.unlink()
        return resolved

    monkeypatch.setattr(archive_module, "_local_plain_path", then_gone)
    client, source = api
    response = client.get(f"/sources/{source.id}/{endpoint}", params={"path": "public/p.log"})
    assert response.status_code == 404
    assert response.json() == _not_found_body()


def test_a_local_file_deleted_while_it_is_sent_is_still_sent_whole(tmp_path):
    """The file served in place is opened when the response is made: deleted
    afterwards, it still goes out whole, as its Content-Length says."""
    path = tmp_path / "app.log"
    content = b"".join(f"line {i}\n".encode() for i in range(200))
    path.write_bytes(content)
    response = archive_module._SnapshotFileResponse(path, media_type="text/plain")
    response.chunk_size = 64
    path.unlink()
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}
    anyio.run(response, scope, receive, send)
    assert messages[0]["status"] == 200
    assert b"".join(m["body"] for m in messages[1:]) == content
    assert response._file.closed


_RANGE_CONTENT = b"".join(f"{i:02d}-line\n".encode() for i in range(10))


def _range_parts(response) -> list[tuple[str, bytes]]:
    """The (Content-Range, body) of each part of a multipart/byteranges answer."""
    content_type = response.headers["content-type"]
    assert content_type.startswith("multipart/byteranges; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts = []
    for raw in response.content.split(b"--" + boundary)[1:]:
        if raw.startswith(b"--"):
            break
        head, _, body = raw.partition(b"\r\n\r\n")
        headers = dict(
            line.decode().split(": ", 1) for line in head.strip(b"\r\n").split(b"\r\n") if line
        )
        assert body.endswith(b"\r\n")
        parts.append((headers["Content-Range"], body[:-2]))
    return parts


@pytest.mark.parametrize("endpoint", ["open", "download"])
def test_a_local_file_served_in_place_answers_range_requests(api, tree, endpoint):  # noqa: F811
    """Range requests are sent from the file opened when the response was made,
    as a single range, a range at the end and several ranges."""
    (tree / "public" / "range.log").write_bytes(_RANGE_CONTENT)
    client, source = api
    url = f"/sources/{source.id}/{endpoint}"
    params = {"path": "public/range.log"}
    size = len(_RANGE_CONTENT)

    single = client.get(url, params=params, headers={"Range": "bytes=10-19"})
    assert single.status_code == 206
    assert single.headers["content-range"] == f"bytes 10-19/{size}"
    assert single.content == _RANGE_CONTENT[10:20]

    suffix = client.get(url, params=params, headers={"Range": "bytes=-5"})
    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == f"bytes {size - 5}-{size - 1}/{size}"
    assert suffix.content == _RANGE_CONTENT[-5:]

    multiple = client.get(url, params=params, headers={"Range": "bytes=0-4, 20-29"})
    assert multiple.status_code == 206
    assert _range_parts(multiple) == [
        (f"bytes 0-4/{size}", _RANGE_CONTENT[0:5]),
        (f"bytes 20-29/{size}", _RANGE_CONTENT[20:30]),
    ]


def test_a_redacted_local_download_deleted_once_prepared_is_still_sent(
    api, session, tree, monkeypatch  # noqa: F811
):
    """A redacted download of a local file reads from the file it opened
    while preparing, not from its path again."""
    _enable_redaction(session)
    prepare = archive_module._prepare_redacted_download

    def then_deleted(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared.path.unlink()
        return prepared

    monkeypatch.setattr(archive_module, "_prepare_redacted_download", then_deleted)
    client, source = api
    response = client.get(f"/sources/{source.id}/download", params={"path": "public/p.log"})
    assert response.status_code == 200
    assert response.content == b"public\n"


# --- SSH -----------------------------------------------------------------------


def _ssh_tree():
    return FakeSFTPClient(
        files={"/var/log/appname/app/a.log": b"hello"},
        symlinks={
            "/var/log/appname/mirror": "/var/log/appname/app",
            "/var/log/appname/aponta": "/var/log/appname/secret",
        },
        dirs={"/var/log/appname", "/var/log/appname/app", "/var/log/appname/secret"},
    )


_HIDE_SECRET = [_rule(0, RuleType.include, "**/*"), _rule(1, RuleType.exclude, "secret/**")]


@pytest.mark.parametrize("path", ["app", "mirror"])
def test_ssh_reads_of_a_folder_are_not_a_file_and_never_open_it(monkeypatch, tmp_path, path):
    sftp = _ssh_tree()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    connector = CONNECTORS[Protocol.ssh]
    with pytest.raises(SourceNotAFileError):
        connector.fetch_file(ssh_source(), path, tmp_path / "out", rules=_HIDE_SECRET)
    with pytest.raises(SourceNotAFileError):
        connector.read_range(ssh_source(), path, 0, 100, rules=_HIDE_SECRET)
    assert sftp.fetched_paths == []
    assert not (tmp_path / "out").exists()


def test_ssh_reads_of_a_link_to_a_hidden_folder_are_link_target_hidden(monkeypatch, tmp_path):
    sftp = _ssh_tree()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    connector = CONNECTORS[Protocol.ssh]
    with pytest.raises(LinkTargetHiddenError):
        connector.fetch_file(ssh_source(), "aponta", tmp_path / "out", rules=_HIDE_SECRET)
    with pytest.raises(LinkTargetHiddenError):
        connector.read_range(ssh_source(), "aponta", 0, 100, rules=_HIDE_SECRET)
    # Without the rules, no link is followed, to a folder or not.
    with pytest.raises(LinkTargetHiddenError):
        ssh_module.fetch_file(ssh_source(), "mirror", tmp_path / "out")
    assert sftp.fetched_paths == []


def _ssh_dangling_tree():
    """x -> secret/missing.log: a link to a path the rules hide, which isn't
    there. The server still resolves it (sftp-server's realpath allows a
    missing last component), but stat() fails on it."""
    return FakeSFTPClient(
        files={"/var/log/appname/app/a.log": b"hello"},
        symlinks={"/var/log/appname/x": "/var/log/appname/secret/missing.log"},
        dirs={"/var/log/appname", "/var/log/appname/app", "/var/log/appname/secret"},
    )


def test_ssh_reads_of_a_dangling_link_to_a_hidden_path_are_link_target_hidden(
    monkeypatch, tmp_path
):
    """The rules are checked on the target before its failed stat() goes out,
    so the refusal is still a hidden link, audited as one."""
    sftp = _ssh_dangling_tree()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    connector = CONNECTORS[Protocol.ssh]
    with pytest.raises(LinkTargetHiddenError):
        connector.fetch_file(ssh_source(), "x", tmp_path / "out", rules=_HIDE_SECRET)
    with pytest.raises(LinkTargetHiddenError):
        connector.read_range(ssh_source(), "x", 0, 100, rules=_HIDE_SECRET)
    # A path that just isn't there is still not found.
    with pytest.raises(SourceNotFoundError):
        connector.fetch_file(ssh_source(), "app/missing.log", tmp_path / "out", rules=_HIDE_SECRET)
    assert sftp.fetched_paths == []


@pytest.fixture()
def ssh_api(api, session, monkeypatch):  # noqa: F811
    """The endpoints over an SSH source on _ssh_dangling_tree, with the
    rules of `api` (`exclude secret/**`)."""
    client, source = api
    source.protocol = Protocol.ssh
    source.base_path = "/var/log/appname"
    source.credential_ref = "encrypted-blob"
    session.add(source)
    session.commit()
    sftp = _ssh_dangling_tree()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    return client, source


@pytest.mark.parametrize(("endpoint", "params", "action", "extra"), _ENDPOINTS)
def test_a_dangling_link_to_a_hidden_path_over_ssh_is_an_audited_404(
    ssh_api, session, endpoint, params, action, extra
):
    client, source = ssh_api
    response = client.get(f"/sources/{source.id}/{endpoint}", params={"path": "x", **params})
    assert response.status_code == 404
    [entry] = _audit(session, action)
    assert entry.event_metadata["path"] == "x"
    assert entry.event_metadata["reason"] == "link_target_hidden"
    for name, value in extra.items():
        assert entry.event_metadata[name] == value


def test_ssh_files_still_read(monkeypatch, tmp_path):
    sftp = _ssh_tree()
    monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))
    CONNECTORS[Protocol.ssh].fetch_file(
        ssh_source(), "mirror/a.log", tmp_path / "out", rules=_HIDE_SECRET
    )
    assert (tmp_path / "out").read_bytes() == b"hello"


# --- SMB -----------------------------------------------------------------------


class _SMBWithFolders(FakeSMBClient):
    def __init__(self, folders):
        super().__init__()
        self.folders = set(folders)

    def stat(self, path, **kwargs):
        if path in self.folders:
            from types import SimpleNamespace

            return SimpleNamespace(st_size=0, st_file_attributes=0x10)
        return super().stat(path, **kwargs)


def test_smb_reads_of_a_folder_are_not_a_file_and_never_open_it(monkeypatch, tmp_path):
    folder = "\\\\fileserver.example.com\\AppLogs\\app"
    fake = _SMBWithFolders({folder})
    monkeypatch.setattr(smb_module, "smbclient", fake)
    monkeypatch.setattr(
        smb_module, "decrypt_credential", lambda ref: {"username": "svc", "password": "s3cret"}
    )
    connector = CONNECTORS[Protocol.smb]
    with pytest.raises(SourceNotAFileError):
        connector.fetch_file(smb_source(), "app", tmp_path / "out")
    with pytest.raises(SourceNotAFileError):
        connector.read_range(smb_source(), "app", 0, 100)
    assert fake.open_file_kwargs == []
    assert not (tmp_path / "out").exists()


def test_smb_file_is_a_directory_status_is_not_a_file():
    from smbprotocol.exceptions import SMBOSError

    error = SMBOSError(0xC00000BA, "\\\\server\\share\\app")
    assert isinstance(translate_smb(error), SourceNotAFileError)
    # Not found stays not found.
    assert type(translate_smb(SMBOSError(0xC0000034, "x"))) is SourceNotFoundError


# --- WinRM ---------------------------------------------------------------------


def _folder(script):
    # What Windows does: the folder check stops the script with its marker.
    if "[IO.Directory]::Exists(" in script:
        return FakeResult(status_code=6, std_out=b"PERCHTAIL_IS_A_DIRECTORY\r\n")
    return FakeResult()


def test_winrm_fetch_of_a_folder_is_not_a_file(fake_session, tmp_path):  # noqa: F811
    fake_session.handler = _folder
    with pytest.raises(SourceNotAFileError):
        CONNECTORS[Protocol.winrm].fetch_file(winrm_source(), "app", tmp_path / "out")
    assert not (tmp_path / "out").exists()
    # Checked after the junction guard, before the file check and the size.
    script = fake_session.scripts_run[0]
    assert (
        script.index("ReparsePoint")
        < script.index("[IO.Directory]::Exists(")
        < script.index("[IO.File]::Exists(")
        < script.index("Get-Item")
    )


def test_winrm_read_range_of_a_folder_is_not_a_file(fake_session):  # noqa: F811
    fake_session.handler = _folder
    with pytest.raises(SourceNotAFileError):
        CONNECTORS[Protocol.winrm].read_range(winrm_source(), "app", 0, 10, 4)
    script = fake_session.scripts_run[0]
    assert script.index("[IO.Directory]::Exists(") < script.index("[IO.File]::Open(")


def test_winrm_a_folder_swapped_in_before_the_read_is_not_a_file(
    fake_session, tmp_path  # noqa: F811
):
    _serve_file(fake_session, b"0123456789")
    fake_session.stream_handler = lambda script: _responses(b"PERCHTAIL_IS_A_DIRECTORY\r\n", 0, 6)
    with pytest.raises(SourceNotAFileError):
        winrm_module.fetch_file(winrm_source(), "app.log", tmp_path / "out")
    assert not (tmp_path / "out").exists()
    (script,) = fake_session.streamed
    assert script.index("[IO.Directory]::Exists(") < script.index("[IO.File]::Open(")
