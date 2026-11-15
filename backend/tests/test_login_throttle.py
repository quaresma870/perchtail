from types import SimpleNamespace

from app import login_throttle
from app.login_throttle import (
    address_key,
    record_failure,
    record_success,
    seconds_until_allowed,
)

IP = "203.0.113.10"
OTHER_IP = "198.51.100.20"


def _patch_settings(monkeypatch, *, max_attempts=3, lockout_seconds=60):
    monkeypatch.setattr(
        login_throttle,
        "get_settings",
        lambda: SimpleNamespace(
            login_max_attempts=max_attempts, login_lockout_seconds=lockout_seconds
        ),
    )


def _fail(username, ip=IP, times=1, **kwargs):
    outcome = None
    for _ in range(times):
        outcome = record_failure(username, ip, **kwargs)
    return outcome


def test_an_unseen_username_and_address_are_allowed():
    assert seconds_until_allowed("nobody@example.com", IP) is None


def test_stays_unlocked_below_the_failure_threshold(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3)

    _fail("user@example.com", times=2)

    assert seconds_until_allowed("user@example.com", IP) is None


def test_locks_out_once_the_failure_threshold_is_reached(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)

    outcome = _fail("user@example.com", times=3)

    # Both limits use LOGIN_MAX_ATTEMPTS, so the address locks too.
    assert outcome.account_locked and outcome.address_locked
    remaining = seconds_until_allowed("user@example.com", IP)
    assert remaining is not None
    assert 0 < remaining <= 60


def test_an_account_lock_applies_from_every_address(monkeypatch):
    # The per-username limit catches guessing spread over many addresses.
    _patch_settings(monkeypatch, max_attempts=3)

    for i in range(3):
        record_failure("user@example.com", f"203.0.113.{i}")

    assert seconds_until_allowed("user@example.com", OTHER_IP) is not None


def test_lock_expires_after_the_configured_duration(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=1, lockout_seconds=10)
    fake_now = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: fake_now[0])

    _fail("user@example.com")
    assert seconds_until_allowed("user@example.com", IP) == 10

    fake_now[0] += 10.001
    assert seconds_until_allowed("user@example.com", IP) is None


def test_record_success_clears_a_pending_lockout(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=2, lockout_seconds=60)

    _fail("user@example.com")
    record_success("user@example.com", IP)
    _fail("user@example.com", OTHER_IP)

    # Only one failure since the reset -- still below the threshold of 2.
    # (Checked from a third address: the address counts aren't cleared.)
    assert seconds_until_allowed("user@example.com", "192.0.2.30") is None


def test_different_usernames_are_tracked_independently(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=1, lockout_seconds=60)

    _fail("alice@example.com")

    assert seconds_until_allowed("alice@example.com", IP) is not None
    assert seconds_until_allowed("bob@example.com", OTHER_IP) is None


# --- per address ------------------------------------------------------------------


def test_an_address_spraying_usernames_is_locked_but_others_are_not(monkeypatch):
    # The address limit is LOGIN_MAX_ATTEMPTS, the same as the account limit.
    _patch_settings(monkeypatch, max_attempts=3)
    limit = 3

    outcomes = [record_failure(f"spray-{i}@example.com", IP) for i in range(limit)]

    assert outcomes[-1].address_locked
    assert not any(o.address_locked for o in outcomes[:-1])
    # Any username, from that address, waits...
    assert seconds_until_allowed("someone@example.com", IP) is not None
    # ...from another address, it signs in.
    assert seconds_until_allowed("someone@example.com", OTHER_IP) is None


def test_a_success_does_not_clear_the_address_count(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3)
    limit = 3

    for i in range(limit - 1):
        record_failure(f"spray-{i}@example.com", IP)
    record_success("valid@example.com", IP)
    record_failure("spray-last@example.com", IP)

    assert seconds_until_allowed("valid@example.com", IP) is not None


def test_ipv6_addresses_are_counted_by_their_64(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=2)

    for i in range(2):
        record_failure(f"spray-{i}@example.com", f"2001:db8:1:2::{i + 1}")

    assert seconds_until_allowed("x@example.com", "2001:db8:1:2:ffff::1") is not None
    assert seconds_until_allowed("x@example.com", "2001:db8:1:3::1") is None


def test_address_key():
    assert address_key("203.0.113.5") == "203.0.113.5"
    assert address_key("::ffff:203.0.113.5") == "203.0.113.5"
    assert address_key("2001:db8::1") == "2001:db8::/64"
    assert address_key(None) == "-"
    assert address_key("testclient") == "testclient"


# --- emergency accounts -----------------------------------------------------------


def test_an_emergency_account_is_locked_only_for_the_failing_address(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3)

    outcome = _fail("admin", IP, times=3, per_address_account=True)

    assert outcome.account_locked
    assert seconds_until_allowed("admin", IP, per_address_account=True) is not None
    assert seconds_until_allowed("admin", OTHER_IP, per_address_account=True) is None


def test_an_emergency_account_still_obeys_the_address_limit(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3)

    for i in range(3):
        record_failure(f"spray-{i}@example.com", IP)

    assert seconds_until_allowed("admin", IP, per_address_account=True) is not None


# --- bounded tables ---------------------------------------------------------------


def test_tracked_usernames_are_bounded(monkeypatch):
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_ACCOUNTS", 2)
    _patch_settings(monkeypatch, max_attempts=5, lockout_seconds=60)

    _fail("first@example.com")
    _fail("second@example.com")
    _fail("third@example.com")

    # Still bounded, but by not tracking the newcomer, never by evicting a
    # live entry (which would reset that account's failure count).
    assert len(login_throttle._accounts.state) == 2
    assert "first@example.com" in login_throttle._accounts.state
    assert "third@example.com" not in login_throttle._accounts.state


def test_flooding_other_usernames_cannot_evict_a_locked_account(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_ACCOUNTS", 5)

    _fail("victim@example.com", times=3)
    for i in range(50):
        record_failure(f"noise-{i}@example.com", f"198.51.100.{i}")

    assert seconds_until_allowed("victim@example.com", OTHER_IP) is not None


def test_flooding_cannot_reset_a_partial_failure_count(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_ACCOUNTS", 5)

    _fail("victim@example.com", times=2)
    for i in range(50):
        record_failure(f"noise-{i}@example.com", f"198.51.100.{i}")
    _fail("victim@example.com")

    assert seconds_until_allowed("victim@example.com", OTHER_IP) is not None


def test_a_full_account_table_refuses_only_by_address(monkeypatch):
    # 10,000 made-up usernames from one address used to fill the table and
    # refuse every new username, from anywhere. Now the flooding address is
    # locked and everyone else signs in.
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_ACCOUNTS", 5)

    for i in range(50):
        record_failure(f"noise-{i}@example.com", IP)

    assert seconds_until_allowed("someone-new@example.com", OTHER_IP) is None
    assert seconds_until_allowed("someone-new@example.com", IP) is not None


def test_a_full_address_table_fails_closed_for_new_addresses(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_ADDRESSES", 5)

    for i in range(5):
        record_failure("someone@example.com", f"198.51.100.{i}")

    assert seconds_until_allowed("x@example.com", "198.51.100.1") is None
    assert seconds_until_allowed("x@example.com", OTHER_IP) is not None


def test_old_failures_expire_and_free_their_slot(monkeypatch):
    _patch_settings(monkeypatch, max_attempts=3, lockout_seconds=60)
    monkeypatch.setattr(login_throttle, "_MAX_TRACKED_ADDRESSES", 5)
    now = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: now[0])

    for i in range(5):
        record_failure(f"noise-{i}@example.com", f"198.51.100.{i}")
    now[0] += 61

    assert seconds_until_allowed("someone-new@example.com", OTHER_IP) is None
