"""Test-only WinRM double for the Playwright E2E suite (see
backend/scripts/run_e2e_server.sh, which sets
PERCHTAIL_TEST_PATCH_MODULE=app.testing.fake_winrm -- app/main.py imports
whatever module that names, once, at startup, only if the env var is set).

Unlike ssh and smb, which the e2e suite points at real local test servers
(backend/scripts/setup_e2e_test_servers.sh), there's no real WinRM target
available in CI or this dev sandbox -- a real one needs an actual Windows
host. This module replaces app.collectors.winrm's `_session` factory (the
same seam backend/tests/test_collectors_winrm.py's own FakeWinRMSession
patches at the unit-test level) with one that serves a small in-memory
fixture "filesystem" instead of talking to a real WSMan endpoint, by
pattern-matching the exact PowerShell script text list_directory() and
fetch_file() generate. Because it's plugged in at that same seam, both
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

_LIST_RE = re.compile(r"Get-ChildItem -LiteralPath '(?P<path>.*)' \|")
_READ_RE = re.compile(
    r"\[Convert\]::ToBase64String\(\[IO\.File\]::ReadAllBytes\('(?P<path>.*)'\)\)"
)


class _FakeResult:
    def __init__(self, std_out: bytes = b"", std_err: bytes = b"", status_code: int = 0):
        self.std_out = std_out
        self.std_err = std_err
        self.status_code = status_code


class _FakeWinRMSession:
    """Only understands the two exact script shapes winrm.py's
    list_directory()/fetch_file() actually generate -- anything else fails
    loudly (non-zero status_code) instead of silently returning nothing, so
    a real change to those scripts gets noticed here too, not masked."""

    def run_ps(self, script: str) -> _FakeResult:
        list_match = _LIST_RE.search(script)
        if list_match:
            return self._list(list_match.group("path"))

        read_match = _READ_RE.search(script)
        if read_match:
            return self._read(read_match.group("path"))

        return _FakeResult(status_code=1, std_err=b"fake_winrm: unrecognized script")

    def _list(self, path: str) -> _FakeResult:
        if path.rstrip("\\") != FIXTURE_ROOT.rstrip("\\"):
            return _FakeResult(status_code=1, std_err=b"fake_winrm: path not found")
        items = [
            {"Name": name, "PSIsContainer": False, "Length": len(content)}
            for name, content in FIXTURE_FILES.items()
        ]
        return _FakeResult(std_out=json.dumps(items).encode("utf-8"))

    def _read(self, path: str) -> _FakeResult:
        name = path.rsplit("\\", 1)[-1]
        content = FIXTURE_FILES.get(name)
        if content is None:
            return _FakeResult(status_code=1, std_err=b"fake_winrm: file not found")
        return _FakeResult(std_out=base64.b64encode(content))


winrm_module._session = lambda source: _FakeWinRMSession()
