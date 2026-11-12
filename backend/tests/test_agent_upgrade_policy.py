"""Remote agent upgrades (#319): the policies and the automatic window."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from app import agent_upgrade_policy as policies
from app import system_settings
from app.agent_upgrade_policy import UpgradeWindow, effective_policy, window_bounds
from app.models import SystemSetting

LISBON = ZoneInfo("Europe/Lisbon")
EVERY_DAY = frozenset(range(7))


def _utc(*args) -> datetime:
    return datetime(*args)


@pytest.mark.parametrize(
    ("source", "system", "expected"),
    [
        ("inherit", "off", "off"),
        ("inherit", "manual", "manual"),
        ("inherit", "automatic", "automatic"),
        (None, "manual", "manual"),
        # A source's own policy wins either way.
        ("automatic", "off", "automatic"),
        ("manual", "off", "manual"),
        ("off", "automatic", "off"),
        ("off", "manual", "off"),
        ("garbage", "manual", "manual"),
        ("inherit", "garbage", "off"),
    ],
)
def test_the_effective_policy(source, system, expected):
    assert effective_policy(source, system) == expected


def test_defaults(session):
    settings = policies.read_settings(session)
    assert settings.policy == "off"
    assert settings.window == UpgradeWindow(days=EVERY_DAY, start_minutes=120, hours=4)
    assert settings.failure_limit == 3
    assert settings.concurrency == 5


def test_the_old_on_off_setting_reads_as_manual(session):
    session.add(SystemSetting(key="agent_remote_upgrade_enabled", value="true"))
    session.commit()
    assert policies.system_policy(session) == "manual"
    # Saving the policy replaces it.
    policies.set_system_policy(session, "automatic")
    session.commit()
    assert session.get(SystemSetting, "agent_remote_upgrade_enabled") is None
    assert policies.system_policy(session) == "automatic"


def test_the_old_setting_switched_off_reads_as_off(session):
    session.add(SystemSetting(key="agent_remote_upgrade_enabled", value="false"))
    session.commit()
    assert policies.system_policy(session) == "off"


def test_a_saved_policy_wins_over_the_old_setting(session):
    session.add(SystemSetting(key="agent_remote_upgrade_enabled", value="true"))
    system_settings.set_choice(session, "agent_upgrade_policy", "off")
    session.commit()
    assert policies.system_policy(session) == "off"


def test_stored_values_out_of_range_are_bounded(session):
    system_settings.set_int(session, "agent_upgrade_failure_limit", 99)
    system_settings.set_int(session, "agent_upgrade_concurrency", 0)
    system_settings.set_int(session, "agent_upgrade_window_hours", 30)
    system_settings.set_int(session, "agent_upgrade_window_start_minutes", 5000)
    session.commit()
    settings = policies.read_settings(session)
    assert settings.failure_limit == 10
    assert settings.concurrency == 1
    assert settings.window.hours == 24
    assert settings.window.start_minutes == 120


def test_days_round_trip_through_the_mask():
    assert policies.days_from_mask(127) == EVERY_DAY
    assert policies.mask_from_days([0, 6]) == 0b1000001
    assert policies.days_from_mask(policies.mask_from_days([1, 3, 5])) == {1, 3, 5}


@pytest.mark.parametrize(
    ("text", "minutes"), [("00:00", 0), ("02:00", 120), ("9:05", 545), ("23:59", 1439)]
)
def test_start_times(text, minutes):
    assert policies.parse_start(text) == minutes


@pytest.mark.parametrize("text", ["24:00", "12:60", "1200", "12:5", "ab:cd", "", "-1:00"])
def test_bad_start_times(text):
    with pytest.raises(ValueError):
        policies.parse_start(text)


def test_a_window_on_a_picked_day():
    # Wednesday 2026-11-11, 02:00 for 4 hours, in UTC.
    window = UpgradeWindow(days=frozenset({2}), start_minutes=120, hours=4)
    assert window_bounds(window, _utc(2026, 11, 11, 1, 59), UTC) is None
    assert window_bounds(window, _utc(2026, 11, 11, 2, 0), UTC) == (
        _utc(2026, 11, 11, 2, 0),
        _utc(2026, 11, 11, 6, 0),
    )
    assert window_bounds(window, _utc(2026, 11, 11, 5, 59), UTC) is not None
    assert window_bounds(window, _utc(2026, 11, 11, 6, 0), UTC) is None  # the end is exclusive
    # Not on the Thursday.
    assert window_bounds(window, _utc(2026, 11, 12, 3, 0), UTC) is None


def test_a_window_past_midnight_belongs_to_the_day_it_starts():
    # Fridays 22:00 for 6 hours: until Saturday 04:00.
    window = UpgradeWindow(days=frozenset({4}), start_minutes=22 * 60, hours=6)
    friday_start = _utc(2026, 11, 13, 22, 0)
    assert window_bounds(window, _utc(2026, 11, 13, 23, 0), UTC) == (
        friday_start,
        _utc(2026, 11, 14, 4, 0),
    )
    assert window_bounds(window, _utc(2026, 11, 14, 3, 59), UTC) == (
        friday_start,
        _utc(2026, 11, 14, 4, 0),
    )
    assert window_bounds(window, _utc(2026, 11, 14, 4, 0), UTC) is None
    # A Saturday window isn't picked, so Saturday 23:00 is outside.
    assert window_bounds(window, _utc(2026, 11, 14, 23, 0), UTC) is None
    # Thursday night isn't a window either.
    assert window_bounds(window, _utc(2026, 11, 13, 1, 0), UTC) is None


def test_a_24_hour_window_every_day_is_always_open():
    window = UpgradeWindow(days=EVERY_DAY, start_minutes=0, hours=24)
    for hour in range(24):
        start, end = window_bounds(window, _utc(2026, 11, 11, hour, 30), UTC)
        assert start == _utc(2026, 11, 11, 0, 0)
        assert end == _utc(2026, 11, 12, 0, 0)


def test_a_24_hour_window_on_one_day_ends_the_next_day():
    window = UpgradeWindow(days=frozenset({2}), start_minutes=12 * 60, hours=24)
    assert window_bounds(window, _utc(2026, 11, 12, 11, 59), UTC) is not None
    assert window_bounds(window, _utc(2026, 11, 12, 12, 0), UTC) is None


def test_the_window_is_in_the_server_time_zone():
    # 02:00 in Lisbon is 01:00 UTC in summer (WEST) and 02:00 UTC in winter.
    window = UpgradeWindow(days=EVERY_DAY, start_minutes=120, hours=1)
    summer = window_bounds(window, _utc(2026, 7, 1, 1, 30), LISBON)
    assert summer == (_utc(2026, 7, 1, 1, 0), _utc(2026, 7, 1, 2, 0))
    winter = window_bounds(window, _utc(2026, 12, 1, 2, 30), LISBON)
    assert winter == (_utc(2026, 12, 1, 2, 0), _utc(2026, 12, 1, 3, 0))
    assert window_bounds(window, _utc(2026, 12, 1, 1, 30), LISBON) is None


def test_the_day_is_the_local_day():
    # Mondays 00:30 in a zone ahead of UTC: Sunday 23:30 UTC is already
    # Monday there.
    tokyo = ZoneInfo("Asia/Tokyo")
    window = UpgradeWindow(days=frozenset({0}), start_minutes=30, hours=2)
    # Monday 2026-11-16 00:30 JST == Sunday 2026-11-15 15:30 UTC.
    assert window_bounds(window, _utc(2026, 11, 15, 16, 0), tokyo) == (
        _utc(2026, 11, 15, 15, 30),
        _utc(2026, 11, 15, 17, 30),
    )


def test_no_day_picked_means_no_window():
    window = UpgradeWindow(days=frozenset(), start_minutes=0, hours=24)
    assert window_bounds(window, _utc(2026, 11, 11, 12, 0), UTC) is None
    assert policies.next_window_start(window, _utc(2026, 11, 11, 12, 0), UTC) is None


def test_the_next_window():
    window = UpgradeWindow(days=frozenset({4}), start_minutes=22 * 60, hours=6)
    # From Wednesday: this Friday.
    assert policies.next_window_start(window, _utc(2026, 11, 11, 12, 0), UTC) == _utc(
        2026, 11, 13, 22, 0
    )
    # From inside Friday's window: the Friday after.
    assert policies.next_window_start(window, _utc(2026, 11, 13, 23, 0), UTC) == _utc(
        2026, 11, 20, 22, 0
    )


def test_the_last_window_is_remembered(session):
    start, end = _utc(2026, 11, 11, 2, 0), _utc(2026, 11, 11, 6, 0)
    assert policies.last_window(session) is None
    assert policies.remember_window(session, start, end) is True
    session.commit()
    assert policies.remember_window(session, start, end) is False
    assert policies.last_window(session) == (start, end)


def test_the_server_time_zone_has_a_name():
    zone, name = policies.server_timezone()
    assert zone is not None
    assert name
