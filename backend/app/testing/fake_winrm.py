"""Test-only WinRM double for the Playwright E2E suite (see
backend/scripts/run_e2e_server.sh, which sets
PERCHTAIL_TEST_PATCH_MODULE=app.testing.fake_winrm -- app/main.py imports
whatever module that names, once, at startup, only if the env var is set).

Unlike ssh and smb, which the e2e suite points at real local test servers
(backend/scripts/setup_e2e_test_servers.sh), there's no real WinRM target
available in CI or this dev sandbox -- a real one needs an actual Windows
host. This module replaces app.collectors.winrm's `_session` factory (the
same seam backend/tests/test_collectors_winrm.py's own fakes patch at the
unit-test level) with one that serves a small in-memory fixture
"filesystem" instead of talking to a real WSMan endpoint, by
pattern-matching the exact PowerShell script text list_directory() and
fetch_file() generate: run_ps for the listing and the size, and the
Protocol calls (open_shell, run_command, get_command_output_raw, ...) for
the streamed read. Because it's plugged in at that same seam, both
app/api/sources.py's connection-check endpoint and app/api/archive.py's
browse/open endpoints exercise the real winrm.py collector code end to
end -- only the actual network call underneath is faked.

FIXTURE_ROOT must match the base_path used when creating the e2e winrm
Source (see frontend/e2e/sources-winrm.spec.ts)."""

import base64
import json
import re

import app.collectors.winrm as winrm_module

FIXTURE_ROOT = "C:\\Logs\\e2e-fixture"
FIXTURE_FILES = {
    "hello.log": b"winrm hello world log line 1\nwinrm hello world log line 2\n",
    "other.log": b"winrm other log\n",
}

_LIST_RE = re.compile(r"Get-ChildItem -LiteralPath '(?P<path>.*)' \|.*ToBase64String")
_SIZE_RE = re.compile(r"\(Get-Item -LiteralPath '(?P<path>.*)'\)\.Length")
_READ_RE = re.compile(
    r"\[IO\.File\]::Open\('(?P<path>.*)', 'Open', 'Read', 'ReadWrite'\); "
    r"try \{ \$left = \[long\](?P<size>\d+);.*New-Object byte\[\] (?P<block>\d+);.*"
    r'"PERCHTAIL_END \$t"',
    re.S,
)
_ENCODED_RE = re.compile(r"^powershell -encodedcommand (?P<script>[A-Za-z0-9+/=]+)$")


class _FakeResult:
    def __init__(self, std_out: bytes = b"", std_err: bytes = b"", status_code: int = 0):
        self.std_out = std_out
        self.std_err = std_err
        self.status_code = status_code


def _content(path: str) -> bytes | None:
    return FIXTURE_FILES.get(path.rsplit("\\", 1)[-1])


class _FakeTransport:
    def close_session(self) -> None:
        pass


class _FakeProtocol:
    """The pywinrm Protocol calls fetch_file's streamed read makes. Only the
    exact read script is understood; anything else ends with a non-zero exit
    code, so a change to the script gets noticed here too."""

    def __init__(self):
        self.transport = _FakeTransport()
        self._output: dict[str, tuple[bytes, int]] = {}

    def open_shell(self) -> str:
        return "shell"

    def run_command(self, shell_id: str, command: str) -> str:
        match = _ENCODED_RE.match(command)
        script = base64.b64decode(match.group("script")).decode("utf-16-le") if match else ""
        self._output["command"] = self._read(script)
        return "command"

    def _read(self, script: str) -> tuple[bytes, int]:
        read_match = _READ_RE.search(script)
        if not read_match:
            return b"", 1
        content = _content(read_match.group("path"))
        if content is None:
            # What the real script's existence check answers (#272).
            return b"PERCHTAIL_NOT_FOUND\r\n", 5
        content = content[: int(read_match.group("size"))]
        block = int(read_match.group("block"))
        lines = [
            base64.b64encode(content[i : i + block]) + b"\r\n"
            for i in range(0, len(content), block)
        ]
        return b"".join(lines) + f"PERCHTAIL_END {len(content)}\r\n".encode(), 0

    def get_command_output_raw(self, shell_id: str, command_id: str):
        out, code = self._output.pop(command_id, (b"", 1))
        err = b"" if code in (0, 5) else b"fake_winrm: unrecognized script"
        return out, err, code, True

    def cleanup_command(self, shell_id: str, command_id: str) -> None:
        pass

    def close_shell(self, shell_id: str, close_session: bool = True) -> None:
        pass


class _FakeWinRMSession:
    """Only understands the exact script shapes winrm.py's
    list_directory()/fetch_file() actually generate -- anything else fails
    loudly (non-zero status_code) instead of silently returning nothing, so
    a real change to those scripts gets noticed here too, not masked."""

    def __init__(self):
        self.protocol = _FakeProtocol()

    def _clean_error_msg(self, msg: bytes) -> bytes:
        return msg

    def run_ps(self, script: str) -> _FakeResult:
        list_match = _LIST_RE.search(script)
        if list_match:
            return self._list(list_match.group("path"))

        size_match = _SIZE_RE.search(script)
        if size_match:
            content = _content(size_match.group("path"))
            if content is None:
                # What the real script's existence check answers (#272).
                return _FakeResult(status_code=5, std_out=b"PERCHTAIL_NOT_FOUND")
            return _FakeResult(std_out=str(len(content)).encode())

        return _FakeResult(status_code=1, std_err=b"fake_winrm: unrecognized script")

    def _list(self, path: str) -> _FakeResult:
        if path.rstrip("\\") != FIXTURE_ROOT.rstrip("\\"):
            return _FakeResult(status_code=1, std_err=b"fake_winrm: path not found")
        items = [
            {"Name": name, "PSIsContainer": False, "Length": len(content)}
            for name, content in FIXTURE_FILES.items()
        ]
        return _FakeResult(std_out=base64.b64encode(json.dumps(items).encode("utf-8")))


winrm_module._session = lambda source, envelope_size=None: _FakeWinRMSession()
