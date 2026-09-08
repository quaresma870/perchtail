from types import SimpleNamespace

from app import login_throttle
from app.login_throttle import record_failure, record_success, seconds_until_unlocked


def _patch_settings(monkeypatch, *, max_attempts=3, lockout_seconds=60):
    monkeypatch.setattr(
        login_throttle,
        "get_settings",
        lambda: SimpleNamespace(
            login_max_attempts=max_attempts, login_lockout_seconds=lockout_seconds
        ),
    )


def test_seconds_until_unlocked_is_none_for_an_unseen_username():
    assert seconds_until_unlocked("nobody@example.com") is None


def test_stays_unlocked_below_the_failure_threshold(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3)

    record_failure("user@example.com")
    record_failure("user@example.com")

    assert seconds_until_unlocked("user@example.com") is None


def test_locks_out_once_the_failure_threshold_is_reached(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)

    for _ in range(3):
        record_failure("user@example.com")

    remaining = seconds_until_unlocked("user@example.com")
    assert remaining is not None
    assert 0 < remaining <= 60


def test_lock_expires_after_the_configured_duration(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=1, lockout_seconds=10)
    fake_now = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: fake_now[0])

    record_failure("user@example.com")
    assert seconds_until_unlocked("user@example.com") == 10

    fake_now[0] += 10.001
    assert seconds_until_unlocked("user@example.com") is None


def test_record_success_clears_a_pending_lockout(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=2, lockout_seconds=60)

    record_failure("user@example.com")
    record_success("user@example.com")
    record_failure("user@example.com")

    # Only one failure since the reset -- still below the threshold of 2.
    assert seconds_until_unlocked("user@example.com") is None


def test_different_usernames_are_tracked_independently(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=1, lockout_seconds=60)

    record_failure("alice@example.com")

    assert seconds_until_unlocked("alice@example.com") is not None
    assert seconds_until_unlocked("bob@example.com") is None


def test_tracked_usernames_are_bounded(monkeypatch):
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_USERNAMES", 2)
    _patch_settings(monkeypatch, max_attempts=5, lockout_seconds=60)

    record_failure("first@example.com")
    record_failure("second@example.com")
    record_failure("third@example.com")

    assert "first@example.com" not in login_throttle._state
    assert "second@example.com" in login_throttle._state
    assert "third@example.com" in login_throttle._state
