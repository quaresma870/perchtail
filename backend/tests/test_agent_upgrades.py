"""Upgrade attempts, the automatic upgrades (#319), and the bundled agent
binaries."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app import agent_upgrade_policy as policies
from app import agent_upgrades, system_settings
from app.agent_registry import AgentCommandError, AgentTimeoutError
from app.agent_upgrades import (
    TIMEOUT_SECONDS,
    UpgradeInProgressError,
    agent_connected,
    begin_run,
    binary_path,
    bundled_binaries,
    expire_stale,
    handle_message,
    record_answer,
    run_scheduled_upgrades,
    start_upgrades,
    window_summary,
)
from app.auth.models import AuditLog
from app.config import get_settings
from app.models import AgentUpgradeRun, Protocol, Source
from app.version import APP_VERSION
from sqlmodel import select


class FakeRegistry:
    """Which sources are connected, and how each agent answers."""

    def __init__(self):
        self.connected: set[int] = set()
        self.answers: dict[int, object] = {}
        self.sent: list[tuple[int, str, dict]] = []

    def is_connected(self, source_id):
        return source_id in self.connected

    def send_command_sync(self, source_id, command_type, timeout=30.0, **fields):
        self.sent.append((source_id, command_type, fields))
        answer = self.answers.get(source_id, {"type": "upgrade_accepted"})
        if isinstance(answer, Exception):
            raise answer
        return {**answer, "id": fields["command_id"]}


@pytest.fixture()
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(agent_upgrades, "get_agent_registry", lambda: fake)
    return fake


def _agent(session, *, version="0.0.1", policy="inherit", failures=0, enabled=True) -> Source:
    source = Source(
        name=f"agent-{uuid.uuid4().hex[:6]}",
        protocol=Protocol.agent,
        host="host01",
        base_path="/var/log",
        agent_version=version,
        agent_upgrade_policy=policy,
        agent_upgrade_failures=failures,
        enabled=enabled,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _audits(session):
    return session.exec(
        select(AuditLog).where(AuditLog.action == "agent.upgrade").order_by(AuditLog.id)
    ).all()


def _run(session, source, *, trigger="manual", now=None) -> AgentUpgradeRun:
    return begin_run(
        session, source.id, source.agent_version, trigger=trigger, user_id=None, now=now
    )


def _accept(session, source, **kwargs) -> AgentUpgradeRun:
    run = _run(session, source, **kwargs)
    record_answer(session, run, "accepted", None, now=kwargs.get("now"))
    session.commit()
    return run


def _progress(session, source, run, stage, **extra):
    assert handle_message(
        session,
        source.id,
        {"type": "upgrade_progress", "id": run.command_id, "stage": stage, **extra},
    )
    session.commit()


def _fresh(session, row):
    session.expire_all()
    return session.get(type(row), row.id)


# --- one attempt ----------------------------------------------------------------


def test_an_attempt_is_stored_and_succeeds_when_the_agent_returns_upgraded(session):
    source = _agent(session)
    run = _accept(session, source)
    assert run.status == "accepted"
    assert (run.from_version, run.to_version, run.trigger) == ("0.0.1", APP_VERSION, "manual")

    _progress(session, source, run, "downloading", detail="from the PerchTail server")
    run = _fresh(session, run)
    assert (run.status, run.message) == ("downloading", "from the PerchTail server")
    _progress(session, source, run, "installing")
    _progress(session, source, run, "restarting")
    assert _fresh(session, run).status == "restarting"

    agent_connected(session, source.id, APP_VERSION)
    session.commit()
    run = _fresh(session, run)
    assert run.status == "succeeded"
    assert run.finished_at is not None
    results = [a.event_metadata["result"] for a in _audits(session)]
    assert results == ["accepted", "succeeded"]


def test_an_agent_back_with_the_old_version_after_restarting_failed(session):
    source = _agent(session)
    run = _accept(session, source)
    _progress(session, source, run, "restarting")
    agent_connected(session, source.id, "0.0.1")
    session.commit()
    run = _fresh(session, run)
    assert run.status == "failed"
    assert "came back with version 0.0.1" in run.message
    assert _fresh(session, source).agent_upgrade_failures == 1


def test_a_reconnect_during_the_download_fails_the_attempt(session):
    source = _agent(session)
    run = _accept(session, source)
    _progress(session, source, run, "downloading")
    agent_connected(session, source.id, "0.0.1")
    session.commit()
    assert "dropped during the upgrade" in _fresh(session, run).message


def test_the_agents_failure_report_ends_the_attempt(session):
    source = _agent(session)
    run = _accept(session, source)
    handle_message(
        session,
        source.id,
        {"type": "upgrade_failed", "id": run.command_id, "error": "doesn't match its SHA-256"},
    )
    session.commit()
    run = _fresh(session, run)
    assert (run.status, run.message) == ("failed", "doesn't match its SHA-256")
    final = _audits(session)[-1].event_metadata
    assert final["result"] == "failed"
    assert final["reason"] == "doesn't match its SHA-256"


def test_messages_about_another_attempt_are_ignored(session):
    source = _agent(session)
    other = _agent(session)
    run = _accept(session, source)
    assert not handle_message(session, source.id, {"type": "upgrade_failed", "id": "x"})
    # The command belongs to another source.
    assert not handle_message(session, other.id, {"type": "upgrade_failed", "id": run.command_id})
    assert not handle_message(session, source.id, {"type": "upgrade_failed"})
    assert _fresh(session, run).status == "accepted"


def test_progress_before_the_answer_is_kept(session):
    # The agent may report progress before the request records its answer.
    source = _agent(session)
    run = _run(session, source)
    _progress(session, source, run, "downloading")
    record_answer(session, _fresh(session, run), "accepted", None)
    session.commit()
    assert _fresh(session, run).status == "downloading"


@pytest.mark.parametrize(
    ("answer", "status", "counted"),
    [
        (
            AgentCommandError(
                "this host doesn't allow upgrades", code="refused", message_type="upgrade_error"
            ),
            "refused",
            True,
        ),
        (AgentCommandError("unknown command", message_type="unknown_error"), "too_old", False),
        (AgentTimeoutError("no answer"), "failed", True),
    ],
)
def test_answers_that_end_the_attempt(session, registry, answer, status, counted):
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = answer
    [started] = start_upgrades(session, [(source.id, "0.0.1")], trigger="manual", user_id=None)
    assert started.result == status
    run = session.get(AgentUpgradeRun, started.run_id)
    assert run.status == status
    assert run.finished_at is not None
    # Audited once, with the answer.
    [audit] = _audits(session)
    assert audit.event_metadata["result"] == status
    assert _fresh(session, source).agent_upgrade_failures == (1 if counted else 0)


def test_one_attempt_at_a_time_per_source(session):
    source = _agent(session)
    other = _agent(session)
    run = _run(session, source)
    with pytest.raises(UpgradeInProgressError):
        _run(session, source)
    _run(session, other)  # another source is fine
    record_answer(session, run, "refused", "no")
    session.commit()
    _run(session, source)  # and again once it ended


def test_an_attempt_without_news_times_out(session):
    source = _agent(session)
    started = datetime(2026, 11, 11, 2, 0)
    run = _accept(session, source, now=started)
    handle_message(
        session,
        source.id,
        {"type": "upgrade_progress", "id": run.command_id, "stage": "restarting"},
        now=started,
    )
    session.commit()

    assert expire_stale(session, started + timedelta(seconds=TIMEOUT_SECONDS - 1)) == 0
    assert expire_stale(session, started + timedelta(seconds=TIMEOUT_SECONDS + 1)) == 1
    session.commit()
    run = _fresh(session, run)
    assert run.status == "timeout"
    assert "didn't come back" in run.message
    assert _fresh(session, source).agent_upgrade_failures == 1
    assert _audits(session)[-1].event_metadata["result"] == "timeout"


def test_news_from_the_agent_keeps_the_attempt_alive(session):
    source = _agent(session)
    started = datetime(2026, 11, 11, 2, 0)
    run = _accept(session, source, now=started)
    later = started + timedelta(seconds=TIMEOUT_SECONDS - 60)
    handle_message(
        session,
        source.id,
        {"type": "upgrade_progress", "id": run.command_id, "stage": "installing"},
        now=later,
    )
    session.commit()
    assert expire_stale(session, started + timedelta(seconds=TIMEOUT_SECONDS + 1)) == 0


def test_a_timed_out_attempt_frees_the_source(session):
    source = _agent(session)
    _run(session, source, now=datetime(2020, 1, 1))
    run = _run(session, source)  # the old one times out first
    assert run.status == "requested"


def test_reconnects_without_an_attempt_change_nothing(session):
    source = _agent(session)
    agent_connected(session, source.id, APP_VERSION)
    session.commit()
    assert session.exec(select(AgentUpgradeRun)).all() == []


# --- failures and suspension -------------------------------------------------------


def _fail(session, source):
    run = _accept(session, source)
    handle_message(session, source.id, {"type": "upgrade_failed", "id": run.command_id})
    session.commit()
    return _fresh(session, run)


def test_consecutive_failures_reach_the_limit_and_success_clears_them(session):
    system_settings.set_int(session, "agent_upgrade_failure_limit", 2)
    session.commit()
    source = _agent(session)
    first = _fail(session, source)
    assert not first.suspended_source
    second = _fail(session, source)
    assert second.suspended_source  # this one suspended the source
    third = _fail(session, source)
    assert not third.suspended_source  # already suspended
    assert _fresh(session, source).agent_upgrade_failures == 3

    run = _accept(session, source)
    agent_connected(session, source.id, APP_VERSION)
    session.commit()
    assert _fresh(session, run).status == "succeeded"
    assert _fresh(session, source).agent_upgrade_failures == 0


# --- automatic upgrades ------------------------------------------------------------

# Wednesday 2026-11-11; the default window is every day 02:00-06:00.
IN_WINDOW = datetime(2026, 11, 11, 3, 0)
OUT_OF_WINDOW = datetime(2026, 11, 11, 12, 0)


@pytest.fixture()
def utc_server(monkeypatch):
    monkeypatch.setattr(policies, "server_timezone", lambda: (UTC, "UTC"))


@pytest.fixture()
def automatic(session, utc_server):
    system_settings.set_choice(session, "agent_upgrade_policy", "automatic")
    session.commit()


def _schedule(session, now=IN_WINDOW):
    asked = run_scheduled_upgrades(now, engine=session.get_bind())
    session.expire_all()
    return asked


def _runs(session, source=None):
    query = select(AgentUpgradeRun)
    if source is not None:
        query = query.where(AgentUpgradeRun.source_id == source.id)
    return session.exec(query.order_by(AgentUpgradeRun.id)).all()


def test_automatic_upgrades_run_only_inside_the_window(session, registry, automatic):
    source = _agent(session)
    registry.connected.add(source.id)
    assert _schedule(session, OUT_OF_WINDOW) == 0
    assert registry.sent == []
    assert _schedule(session, IN_WINDOW) == 1
    [run] = _runs(session, source)
    assert (run.trigger, run.requested_by_user_id, run.status) == ("automatic", None, "accepted")
    [audit] = _audits(session)
    assert audit.user_id is None
    assert audit.event_metadata["trigger"] == "automatic"


def test_automatic_upgrades_follow_each_sources_policy(session, registry, utc_server):
    # The system is off, but a source set to automatic still upgrades; one
    # set to off doesn't with the system automatic.
    own = _agent(session, policy="automatic")
    following = _agent(session)
    manual = _agent(session, policy="manual")
    registry.connected.update({own.id, following.id, manual.id})
    _schedule(session)
    assert {s for s, _c, _f in registry.sent} == {own.id}

    system_settings.set_choice(session, "agent_upgrade_policy", "automatic")
    session.commit()
    off = _agent(session, policy="off")
    registry.connected.add(off.id)
    _schedule(session)
    assert {s for s, _c, _f in registry.sent} == {own.id, following.id}


def test_only_connected_outdated_enabled_agents(session, registry, automatic):
    outdated = _agent(session)
    current = _agent(session, version=APP_VERSION)
    offline = _agent(session)
    disabled = _agent(session, enabled=False)
    registry.connected.update({outdated.id, current.id, disabled.id})
    assert offline.id not in registry.connected
    _schedule(session)
    assert [s for s, _c, _f in registry.sent] == [outdated.id]


def test_at_most_the_concurrency_at_a_time(session, registry, automatic):
    system_settings.set_int(session, "agent_upgrade_concurrency", 2)
    session.commit()
    sources = [_agent(session) for _ in range(5)]
    registry.connected.update(s.id for s in sources)
    assert _schedule(session) == 2
    # Both still under way: no room for more.
    assert _schedule(session, IN_WINDOW + timedelta(minutes=1)) == 0
    # One finishes: one more starts.
    first = _runs(session)[0]
    agent_connected(session, first.source_id, APP_VERSION, now=IN_WINDOW + timedelta(minutes=1))
    session.commit()
    assert _schedule(session, IN_WINDOW + timedelta(minutes=2)) == 1
    assert len(registry.sent) == 3


def test_manual_attempts_do_not_take_automatic_slots(session, registry, automatic):
    system_settings.set_int(session, "agent_upgrade_concurrency", 1)
    session.commit()
    manual, auto = _agent(session), _agent(session)
    registry.connected.update({manual.id, auto.id})
    _accept(session, manual)
    assert _schedule(session) == 1
    assert [s for s, _c, _f in registry.sent] == [auto.id]


def test_a_failed_agent_is_not_retried_in_the_same_window(session, registry, automatic):
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = AgentTimeoutError("no answer")
    assert _schedule(session) == 1
    assert _schedule(session, IN_WINDOW + timedelta(minutes=1)) == 0
    assert len(registry.sent) == 1
    # The next day's window tries again.
    assert _schedule(session, IN_WINDOW + timedelta(days=1)) == 1


def test_a_too_old_agent_is_not_asked_again_while_it_runs_the_same_version(
    session, registry, automatic
):
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = AgentCommandError("unknown", message_type="unknown_error")
    assert _schedule(session) == 1
    assert _schedule(session, IN_WINDOW + timedelta(days=1)) == 0
    # Upgraded by hand to a version that knows the command (still outdated).
    source = session.get(Source, source.id)
    source.agent_version = "0.0.2"
    session.add(source)
    session.commit()
    registry.answers.pop(source.id)
    assert _schedule(session, IN_WINDOW + timedelta(days=2)) == 1


def test_a_suspended_source_is_skipped_until_resumed(session, registry, automatic):
    system_settings.set_int(session, "agent_upgrade_failure_limit", 2)
    session.commit()
    source = _agent(session, failures=2)
    registry.connected.add(source.id)
    assert _schedule(session) == 0
    # Only clearing the count resumes it.
    source = session.get(Source, source.id)
    source.agent_upgrade_failures = 0
    session.add(source)
    session.commit()
    assert _schedule(session) == 1


def test_failures_in_automatic_windows_suspend_the_source(session, registry, automatic):
    system_settings.set_int(session, "agent_upgrade_failure_limit", 2)
    session.commit()
    source = _agent(session)
    registry.connected.add(source.id)
    registry.answers[source.id] = AgentCommandError(
        "PERCHTAIL_REMOTE_UPGRADE=false", code="refused", message_type="upgrade_error"
    )
    for day in range(4):
        _schedule(session, IN_WINDOW + timedelta(days=day))
    # Two windows, then suspended.
    assert len(registry.sent) == 2
    assert session.get(Source, source.id).agent_upgrade_failures == 2


def test_the_last_window_is_summarised(session, registry, automatic):
    system_settings.set_int(session, "agent_upgrade_failure_limit", 1)
    session.commit()
    good, bad, running = _agent(session), _agent(session), _agent(session)
    registry.connected.update({good.id, bad.id, running.id})
    registry.answers[bad.id] = AgentTimeoutError("no answer")
    assert window_summary(session) is None
    _schedule(session)
    agent_connected(session, good.id, APP_VERSION, now=IN_WINDOW + timedelta(minutes=5))
    session.commit()
    summary = window_summary(session)
    assert summary["start"] == datetime(2026, 11, 11, 2, 0)
    assert summary["end"] == datetime(2026, 11, 11, 6, 0)
    assert (summary["upgraded"], summary["failed"], summary["suspended"]) == (1, 1, 1)
    assert summary["running"] == 1


def test_no_window_is_recorded_without_automatic_sources(session, registry, utc_server):
    source = _agent(session)
    registry.connected.add(source.id)
    _schedule(session)
    assert window_summary(session) is None


def test_the_job_ends_timed_out_attempts_outside_the_window(session, registry, utc_server):
    source = _agent(session)
    _accept(session, source, now=OUT_OF_WINDOW - timedelta(hours=1))
    _schedule(session, OUT_OF_WINDOW)
    assert _runs(session, source)[0].status == "timeout"


# --- the bundled binaries ------------------------------------------------------------


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
