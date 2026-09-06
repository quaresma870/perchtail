import base64
import importlib

import app.collectors.winrm as winrm_module
import pytest


@pytest.fixture()
def fake_winrm_module(monkeypatch):
    """Importing (or reloading) app.testing.fake_winrm has the side effect
    of monkeypatching app.collectors.winrm._session at module scope --
    that's the whole point of the module in production use (see
    backend/scripts/run_e2e_server.sh), but it must NOT leak into other
    tests in this same pytest process. Captured and restored via
    `monkeypatch` (not a manual try/finally) so it's undone automatically
    even if a test fails outright, the same guarantee monkeypatch already
    gives every other fixture in this test suite."""
    import app.testing.fake_winrm as module

    original_session = winrm_module._session
    importlib.reload(module)
    monkeypatch.setattr(winrm_module, "_session", original_session)
    return module


def test_list_matches_the_real_list_directory_script_shape(fake_winrm_module):
    session = fake_winrm_module._FakeWinRMSession()
    script = (
        f"Get-ChildItem -LiteralPath {winrm_module._ps_quote(fake_winrm_module.FIXTURE_ROOT)} | "
        "Select-Object Name, PSIsContainer, Length | ConvertTo-Json -Compress"
    )
    result = session.run_ps(script)
    assert result.status_code == 0
    assert b'"Name": "hello.log"' in result.std_out or b'"Name":"hello.log"' in result.std_out
    assert b"other.log" in result.std_out


def test_list_fails_for_an_unknown_path(fake_winrm_module):
    session = fake_winrm_module._FakeWinRMSession()
    bad_path = fake_winrm_module.FIXTURE_ROOT + chr(92) + "nope"
    script = (
        f"Get-ChildItem -LiteralPath {winrm_module._ps_quote(bad_path)} | "
        "Select-Object Name, PSIsContainer, Length | ConvertTo-Json -Compress"
    )
    result = session.run_ps(script)
    assert result.status_code != 0


def test_read_matches_the_real_fetch_file_script_shape_and_returns_fixture_content(
    fake_winrm_module,
):
    session = fake_winrm_module._FakeWinRMSession()
    path = f"{fake_winrm_module.FIXTURE_ROOT}\\hello.log"
    script = f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({winrm_module._ps_quote(path)}))"
    result = session.run_ps(script)
    assert result.status_code == 0
    assert base64.b64decode(result.std_out) == fake_winrm_module.FIXTURE_FILES["hello.log"]


def test_read_fails_for_an_unknown_file(fake_winrm_module):
    session = fake_winrm_module._FakeWinRMSession()
    path = f"{fake_winrm_module.FIXTURE_ROOT}\\does-not-exist.log"
    script = f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({winrm_module._ps_quote(path)}))"
    result = session.run_ps(script)
    assert result.status_code != 0


def test_unrecognized_script_fails_loudly_instead_of_returning_nothing(fake_winrm_module):
    session = fake_winrm_module._FakeWinRMSession()
    result = session.run_ps("Get-Process")
    assert result.status_code != 0


def test_importing_the_module_patches_the_real_collectors_session_factory(monkeypatch):
    import app.testing.fake_winrm as module

    original_session = winrm_module._session
    importlib.reload(module)
    try:
        assert winrm_module._session(source=None).__class__.__name__ == "_FakeWinRMSession"
    finally:
        monkeypatch.setattr(winrm_module, "_session", original_session)
