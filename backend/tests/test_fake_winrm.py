import base64

import app.collectors.winrm as winrm_module
from app.testing.fake_winrm import FIXTURE_FILES, FIXTURE_ROOT, _FakeWinRMSession


def test_list_matches_the_real_list_directory_script_shape():
    session = _FakeWinRMSession()
    script = (
        f"Get-ChildItem -LiteralPath {winrm_module._ps_quote(FIXTURE_ROOT)} | "
        "Select-Object Name, PSIsContainer, Length | ConvertTo-Json -Compress"
    )
    result = session.run_ps(script)
    assert result.status_code == 0
    assert b'"Name": "hello.log"' in result.std_out or b'"Name":"hello.log"' in result.std_out
    assert b"other.log" in result.std_out


def test_list_fails_for_an_unknown_path():
    session = _FakeWinRMSession()
    script = (
        f"Get-ChildItem -LiteralPath {winrm_module._ps_quote(FIXTURE_ROOT + chr(92) + 'nope')} | "
        "Select-Object Name, PSIsContainer, Length | ConvertTo-Json -Compress"
    )
    result = session.run_ps(script)
    assert result.status_code != 0


def test_read_matches_the_real_fetch_file_script_shape_and_returns_fixture_content():
    session = _FakeWinRMSession()
    path = f"{FIXTURE_ROOT}\\hello.log"
    script = f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({winrm_module._ps_quote(path)}))"
    result = session.run_ps(script)
    assert result.status_code == 0
    assert base64.b64decode(result.std_out) == FIXTURE_FILES["hello.log"]


def test_read_fails_for_an_unknown_file():
    session = _FakeWinRMSession()
    path = f"{FIXTURE_ROOT}\\does-not-exist.log"
    script = f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({winrm_module._ps_quote(path)}))"
    result = session.run_ps(script)
    assert result.status_code != 0


def test_unrecognized_script_fails_loudly_instead_of_returning_nothing():
    session = _FakeWinRMSession()
    result = session.run_ps("Get-Process")
    assert result.status_code != 0


def test_importing_the_module_patches_the_real_collectors_session_factory():
    # Deliberately not using monkeypatch.setattr for the *import* itself --
    # fake_winrm.py's whole point is a module-level side effect on import,
    # not something monkeypatch can intercept. Restores winrm_module._session
    # by hand afterward so this doesn't leak into other tests in the same
    # pytest process (unlike production use, where this is meant to stick
    # for the life of the e2e server process).
    import importlib

    import app.testing.fake_winrm as fake_winrm_module

    original_session = winrm_module._session
    try:
        importlib.reload(fake_winrm_module)
        assert winrm_module._session(source=None).__class__.__name__ == "_FakeWinRMSession"
    finally:
        winrm_module._session = original_session
