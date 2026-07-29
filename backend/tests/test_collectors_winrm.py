import base64
import json

import app.collectors.winrm as winrm_module
import pytest
from app.models import PatternKind, Protocol, Rule, RuleType, Source


class FakeResult:
    def __init__(self, status_code: int = 0, std_out: bytes = b"", std_err: bytes = b""):
        self.status_code = status_code
        self.std_out = std_out
        self.std_err = std_err


class FakeWinRMSession:
    def __init__(self):
        self.scripts_run: list[str] = []
        self.handler = lambda script: FakeResult()

    def run_ps(self, script):
        self.scripts_run.append(script)
        return self.handler(script)


@pytest.fixture()
def fake_session(monkeypatch):
    session = FakeWinRMSession()
    monkeypatch.setattr(winrm_module, "_session", lambda source: session)
    monkeypatch.setattr(
        winrm_module, "decrypt_credential", lambda ref: {"username": "svc", "password": "s3cret"}
    )
    return session


def _source(**overrides) -> Source:
    defaults = dict(
        name="win01",
        protocol=Protocol.winrm,
        host="win01.example.com",
        base_path="C:\\Logs\\AppName",
        credential_ref="encrypted",
    )
    defaults.update(overrides)
    return Source(**defaults)


def _rule(order, type_, pattern, kind=PatternKind.glob) -> Rule:
    return Rule(source_id=1, order=order, type=type_, pattern=pattern, pattern_kind=kind)


def test_ps_quote_escapes_single_quotes():
    assert winrm_module._ps_quote("it's a test") == "'it''s a test'"


def test_remote_path_joins_base_and_relative():
    source = _source()
    assert winrm_module._remote_path(source) == "C:\\Logs\\AppName"
    assert (
        winrm_module._remote_path(source, "nested/app.log") == "C:\\Logs\\AppName\\nested\\app.log"
    )


def test_list_directory_always_shows_dirs_but_filters_files(fake_session):
    payload = json.dumps(
        [
            {"Name": "nested", "PSIsContainer": True, "Length": None},
            {"Name": "app.log", "PSIsContainer": False, "Length": 100},
            {"Name": "secret.txt", "PSIsContainer": False, "Length": 5},
        ]
    ).encode()
    fake_session.handler = lambda script: FakeResult(std_out=payload)

    rules = [_rule(0, RuleType.include, "*.log")]
    entries = {e.name: e for e in winrm_module.list_directory(_source(), rules)}

    assert "nested" in entries and entries["nested"].is_dir is True
    assert "app.log" in entries and entries["app.log"].size == 100
    assert "secret.txt" not in entries


def test_list_directory_handles_a_single_result_object(fake_session):
    # Get-ChildItem | ConvertTo-Json emits a bare object, not a list, when
    # there's exactly one match.
    payload = json.dumps({"Name": "app.log", "PSIsContainer": False, "Length": 10}).encode()
    fake_session.handler = lambda script: FakeResult(std_out=payload)

    entries = winrm_module.list_directory(_source(), [_rule(0, RuleType.include, "*.log")])
    assert entries[0].name == "app.log"


def test_list_directory_empty_output_returns_empty_list(fake_session):
    fake_session.handler = lambda script: FakeResult(std_out=b"")
    assert winrm_module.list_directory(_source(), []) == []


def test_run_ps_raises_on_nonzero_status(fake_session):
    fake_session.handler = lambda script: FakeResult(status_code=1, std_err=b"boom")
    with pytest.raises(RuntimeError):
        winrm_module._run_ps(_source(), "whatever")


def test_fetch_file_decodes_base64_output(fake_session, tmp_path):
    content = b"hello world"
    fake_session.handler = lambda script: FakeResult(std_out=base64.b64encode(content))

    destination = tmp_path / "out"
    winrm_module.fetch_file(_source(), "app.log", destination)
    assert destination.read_bytes() == content


def test_local_copy_fetches_into_a_temp_path(fake_session):
    content = b"hello world"
    fake_session.handler = lambda script: FakeResult(std_out=base64.b64encode(content))

    with winrm_module.local_copy(_source(), "app.log") as path:
        assert path.read_bytes() == content
