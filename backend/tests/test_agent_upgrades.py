"""The upgrade tracker and the bundled agent binaries (#319)."""

import threading

import pytest
from app import agent_upgrades
from app.agent_upgrades import (
    AgentUpgrades,
    UpgradeInProgressError,
    binary_path,
    bundled_binaries,
    final_audit_metadata,
)
from app.config import get_settings
from app.version import APP_VERSION


class Recorder:
    def __init__(self):
        self.states = []
        self.event = threading.Event()

    def __call__(self, state):
        self.states.append(state)
        self.event.set()

    def wait(self):
        assert self.event.wait(2), "no final audit was written"
        return self.states[-1]


@pytest.fixture()
def tracker():
    recorder = Recorder()
    upgrades = AgentUpgrades(audit_writer=recorder)
    upgrades.recorder = recorder
    return upgrades


def _begin(tracker, source_id=1, command_id="c1"):
    return tracker.begin(
        source_id, user_id=7, command_id=command_id, from_version="0.7.0", to_version="0.7.1"
    )


def test_an_upgrade_runs_and_succeeds_when_the_agent_returns_with_the_new_version(tracker):
    _begin(tracker)
    assert tracker.get(1).status == "requested"
    tracker.accepted(1, "c1")
    assert tracker.get(1).status == "running"

    assert tracker.handle_message(
        1,
        {
            "type": "upgrade_progress",
            "id": "c1",
            "stage": "downloading",
            "detail": "from the PerchTail server",
        },
    )
    state = tracker.get(1)
    assert (state.status, state.stage, state.detail) == (
        "running",
        "downloading",
        "from the PerchTail server",
    )
    tracker.handle_message(1, {"type": "upgrade_progress", "id": "c1", "stage": "restarting"})
    assert tracker.get(1).status == "restarting"

    tracker.agent_connected(1, "0.7.1")
    state = tracker.get(1)
    assert state.status == "succeeded"
    assert state.finished_at is not None
    audited = tracker.recorder.wait()
    assert final_audit_metadata(audited) == {"from": "0.7.0", "to": "0.7.1", "result": "succeeded"}


def test_an_agent_back_with_the_old_version_after_restarting_failed(tracker):
    _begin(tracker)
    tracker.accepted(1, "c1")
    tracker.handle_message(1, {"type": "upgrade_progress", "id": "c1", "stage": "restarting"})
    tracker.agent_connected(1, "0.7.0")
    state = tracker.get(1)
    assert state.status == "failed"
    assert "came back with version 0.7.0" in state.detail
    assert final_audit_metadata(tracker.recorder.wait())["result"] == "failed"


def test_a_reconnect_during_the_download_fails_the_upgrade(tracker):
    _begin(tracker)
    tracker.accepted(1, "c1")
    tracker.agent_connected(1, "0.7.0")
    assert tracker.get(1).status == "failed"
    assert "dropped during the upgrade" in tracker.get(1).detail


def test_the_agents_failure_report_ends_the_upgrade(tracker):
    _begin(tracker)
    tracker.accepted(1, "c1")
    tracker.handle_message(
        1, {"type": "upgrade_failed", "id": "c1", "error": "doesn't match its SHA-256"}
    )
    state = tracker.get(1)
    assert state.status == "failed"
    assert state.detail == "doesn't match its SHA-256"
    audited = tracker.recorder.wait()
    assert final_audit_metadata(audited) == {
        "from": "0.7.0",
        "to": "0.7.1",
        "result": "failed",
        "reason": "doesn't match its SHA-256",
    }


def test_messages_about_another_command_are_ignored(tracker):
    _begin(tracker)
    assert not tracker.handle_message(1, {"type": "upgrade_failed", "id": "other", "error": "x"})
    assert not tracker.handle_message(2, {"type": "upgrade_failed", "id": "c1", "error": "x"})
    assert tracker.get(1).status == "requested"


def test_a_refusal_is_not_audited_twice(tracker):
    # The endpoint audits a refusal with the request itself.
    _begin(tracker)
    tracker.refused(1, "c1", "host opted out")
    assert tracker.get(1).status == "failed"
    assert tracker.recorder.states == []


def test_one_upgrade_at_a_time_per_source(tracker):
    _begin(tracker)
    with pytest.raises(UpgradeInProgressError):
        _begin(tracker, command_id="c2")
    _begin(tracker, source_id=2, command_id="c3")  # another source is fine
    tracker.refused(1, "c1", "no")
    _begin(tracker, command_id="c4")  # and again once it ended


def test_an_upgrade_without_news_times_out(tracker, monkeypatch):
    _begin(tracker)
    tracker.accepted(1, "c1")
    tracker.handle_message(1, {"type": "upgrade_progress", "id": "c1", "stage": "restarting"})
    monkeypatch.setattr(agent_upgrades, "STALL_SECONDS", -1)
    state = tracker.get(1)
    assert state.status == "failed"
    assert "didn't come back" in state.detail
    assert tracker.recorder.wait().status == "failed"


def test_reconnects_without_an_upgrade_change_nothing(tracker):
    tracker.agent_connected(1, "0.7.1")
    assert tracker.get(1) is None


def _bundle(tmp_path, version=APP_VERSION, *, signed=True):
    name = f"perchtail-agent-{version}-linux-amd64"
    (tmp_path / name).write_bytes(b"binary")
    (tmp_path / f"perchtail-agent-{version}-windows-amd64.exe").write_bytes(b"exe")
    (tmp_path / "SHA256SUMS").write_text(f"{'ab' * 32}  {name}\n", encoding="utf-8")
    if signed:
        (tmp_path / "SHA256SUMS.sig").write_bytes(b"s" * 64)
    (tmp_path / "notes.txt").write_text("not served", encoding="utf-8")
    return name


@pytest.fixture()
def binaries_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_binaries_dir", str(tmp_path))
    return tmp_path


def test_bundled_binaries_of_this_version_are_reported(binaries_dir):
    _bundle(binaries_dir)
    assert bundled_binaries() == (True, None)


def test_no_bundled_binaries_is_reported_with_the_reason(binaries_dir, monkeypatch):
    ok, reason = bundled_binaries()
    assert not ok and "No agent binaries are bundled" in reason

    _bundle(binaries_dir, signed=False)
    ok, reason = bundled_binaries()
    assert not ok and "No agent binaries are bundled" in reason

    monkeypatch.setattr(get_settings(), "agent_binaries_dir", "")
    ok, reason = bundled_binaries()
    assert not ok and "AGENT_BINARIES_DIR" in reason


def test_binaries_of_another_version_are_not_offered(binaries_dir):
    _bundle(binaries_dir, version="0.0.1")
    ok, reason = bundled_binaries()
    assert not ok and APP_VERSION in reason
    assert binary_path("perchtail-agent-0.0.1-linux-amd64") is None


def test_only_the_release_files_are_served(binaries_dir):
    name = _bundle(binaries_dir)
    assert binary_path(name) == binaries_dir / name
    assert binary_path(f"perchtail-agent-{APP_VERSION}-windows-amd64.exe") is not None
    assert binary_path("SHA256SUMS") is not None
    assert binary_path("SHA256SUMS.sig") is not None
    for refused in ("notes.txt", "../SHA256SUMS", f"../{name}", "", ".", "SHA256SUMS/.."):
        assert binary_path(refused) is None, refused


def test_a_link_in_the_binaries_folder_is_not_served(binaries_dir, tmp_path_factory):
    # Only files the folder itself holds: a link out of it isn't followed.
    outside = tmp_path_factory.mktemp("outside") / "secret"
    outside.write_text("not for agents", encoding="utf-8")
    (binaries_dir / "SHA256SUMS").symlink_to(outside)
    assert binary_path("SHA256SUMS") is None
