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

    # Still bounded, but by refusing the newcomer, never by evicting a live
    # entry (which would reset that account's failure count).
    assert len(login_throttle._state) == 2
    assert "first@example.com" in login_throttle._state
    assert "third@example.com" not in login_throttle._state


def test_flooding_other_usernames_cannot_evict_a_locked_account(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_USERNAMES", 5)

    for _ in range(3):
        record_failure("victim@example.com")
    for i in range(50):
        record_failure(f"noise-{i}@example.com")

    assert seconds_until_unlocked("victim@example.com") is not None


def test_flooding_cannot_reset_a_partial_failure_count(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_USERNAMES", 5)

    record_failure("victim@example.com")
    record_failure("victim@example.com")
    for i in range(50):
        record_failure(f"noise-{i}@example.com")
    record_failure("victim@example.com")

    assert seconds_until_unlocked("victim@example.com") is not None


def test_a_saturated_tracker_fails_closed_for_new_usernames(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_USERNAMES", 5)

    for i in range(5):
        record_failure(f"noise-{i}@example.com")

    assert seconds_until_unlocked("someone-new@example.com") is not None


def test_old_failures_expire_and_free_their_slot(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_USERNAMES", 5)
    now = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: now[0])

    for i in range(5):
        record_failure(f"noise-{i}@example.com")
    now[0] += 61

    assert seconds_until_unlocked("someone-new@example.com") is None
