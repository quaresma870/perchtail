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
    # Registered with monkeypatch BEFORE the import/reload patches it, so
    # teardown restores the real factory (registering afterwards recorded
    # the fake as the value to restore, and it leaked into later tests).
    original_session = winrm_module._session
    monkeypatch.setattr(winrm_module, "_session", original_session)
    import app.testing.fake_winrm as module

    importlib.reload(module)
    winrm_module._session = original_session
    return module


def test_list_matches_the_real_list_directory_script_shape(fake_winrm_module, monkeypatch):
    """Drives the real collector, so the fake can't drift from the script
    list_directory actually sends."""
    from app.models import PatternKind, Protocol, Rule, RuleType, Source

    session = fake_winrm_module._FakeWinRMSession()
    monkeypatch.setattr(winrm_module, "_session", lambda source: session)
    source = Source(
        name="win",
        protocol=Protocol.winrm,
        host="win",
        base_path=fake_winrm_module.FIXTURE_ROOT,
        credential_ref="x",
    )
    rules = [
        Rule(
            source_id=1,
            order=0,
            type=RuleType.include,
            pattern="**/*",
            pattern_kind=PatternKind.glob,
        )
    ]

    names = {e.name for e in winrm_module.list_directory(source, rules)}

    assert {"hello.log", "other.log"} <= names


def test_list_fails_for_an_unknown_path(fake_winrm_module):
    session = fake_winrm_module._FakeWinRMSession()
    bad_path = fake_winrm_module.FIXTURE_ROOT + chr(92) + "nope"
    script = (
        f"Get-ChildItem -LiteralPath {winrm_module._ps_quote(bad_path)} | "
        "Select-Object Name, PSIsContainer, Length | ConvertTo-Json -Compress"
    )
    result = session.run_ps(script)
    assert result.status_code != 0


def test_fake_serves_the_real_fetch_file_scripts(fake_winrm_module, monkeypatch, tmp_path):
    # Drives winrm.fetch_file() itself (size query + byte-range reads) through
    # the fake, so the fake can't drift from the scripts the collector sends.
    from app.models import Protocol, Source

    monkeypatch.setattr(winrm_module, "_CHUNK_BYTES", 16)
    monkeypatch.setattr(
        winrm_module, "_session", lambda source: fake_winrm_module._FakeWinRMSession()
    )
    source = Source(
        name="w",
        protocol=Protocol.winrm,
        host="h",
        base_path=fake_winrm_module.FIXTURE_ROOT,
        credential_ref="x",
    )
    destination = tmp_path / "out"

    winrm_module.fetch_file(source, "hello.log", destination)

    assert destination.read_bytes() == fake_winrm_module.FIXTURE_FILES["hello.log"]


def test_read_fails_for_an_unknown_file(fake_winrm_module):
    session = fake_winrm_module._FakeWinRMSession()
    path = f"{fake_winrm_module.FIXTURE_ROOT}\\does-not-exist.log"
    result = session.run_ps(f"(Get-Item -LiteralPath {winrm_module._ps_quote(path)}).Length")
    assert result.status_code != 0


def test_unrecognized_script_fails_loudly_instead_of_returning_nothing(fake_winrm_module):
    session = fake_winrm_module._FakeWinRMSession()
    result = session.run_ps("Get-Process")
    assert result.status_code != 0


def test_importing_the_module_patches_the_real_collectors_session_factory(monkeypatch):
    monkeypatch.setattr(winrm_module, "_session", winrm_module._session)
    import app.testing.fake_winrm as module

    importlib.reload(module)
    assert winrm_module._session(source=None).__class__.__name__ == "_FakeWinRMSession"
