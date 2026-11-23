"""When push agents may be upgraded from the portal (#319): the system-wide
policy, each source's own, and the window automatic upgrades run in.

Settings -> Agents sets the policy for every agent -- "off" (the default),
"manual" (users who can manage sources upgrade them) or "automatic" (the
schedule also upgrades them inside the window) -- and each source can follow
it ("inherit", the default) or have its own, either way: a source can upgrade
with the system off, or be off with the system automatic.

The window is a set of days of the week, a start time and a duration, in the
server's local time zone, and may run past midnight into the next day."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Literal

from sqlmodel import Session

from app import system_settings
from app.logging_config import get_logger
from app.models import SystemSetting

logger = get_logger(__name__)

SystemPolicy = Literal["off", "manual", "automatic"]
SourcePolicy = Literal["inherit", "off", "manual", "automatic"]
SYSTEM_POLICIES: tuple[str, ...] = ("off", "manual", "automatic")
SOURCE_POLICIES: tuple[str, ...] = ("inherit", *SYSTEM_POLICIES)

# The setting before the policy existed: true meant what "manual" means now.
LEGACY_SETTING = "agent_remote_upgrade_enabled"
# Where the last automatic window's bounds are kept, for its summary.
LAST_WINDOW_SETTING = "agent_upgrade_last_window"

FAILURE_LIMIT_MIN, FAILURE_LIMIT_MAX = 1, 10
CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 50
WINDOW_HOURS_MIN, WINDOW_HOURS_MAX = 1, 24
ALL_DAYS = frozenset(range(7))


@dataclass(frozen=True)
class UpgradeWindow:
    # 0 is Monday, as datetime.weekday().
    days: frozenset[int]
    # Minutes after midnight, local time.
    start_minutes: int
    hours: int

    @property
    def start_text(self) -> str:
        return f"{self.start_minutes // 60:02d}:{self.start_minutes % 60:02d}"


@dataclass(frozen=True)
class UpgradeSettings:
    policy: str
    window: UpgradeWindow
    failure_limit: int
    concurrency: int


def days_from_mask(mask: int) -> frozenset[int]:
    return frozenset(day for day in range(7) if mask & (1 << day))


def mask_from_days(days) -> int:
    return sum(1 << day for day in set(days) if 0 <= day < 7)


def parse_start(text: str) -> int:
    """Minutes after midnight from "HH:MM"; ValueError otherwise."""
    hours, sep, minutes = text.partition(":")
    if not sep or len(hours) not in (1, 2) or len(minutes) != 2:
        raise ValueError("Use HH:MM, for example 02:00")
    if not (hours.isdigit() and minutes.isdigit()):
        raise ValueError("Use HH:MM, for example 02:00")
    h, m = int(hours), int(minutes)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("Use a time from 00:00 to 23:59")
    return h * 60 + m


def system_policy(session: Session) -> str:
    """The system-wide policy. A database from before it, with the old
    on/off setting switched on and no policy saved, reads as "manual"."""
    if session.get(SystemSetting, "agent_upgrade_policy") is None:
        legacy = session.get(SystemSetting, LEGACY_SETTING)
        if legacy is not None and legacy.value == "true":
            return "manual"
    return system_settings.get_choice(session, "agent_upgrade_policy")


def _bounded(value: int, low: int, high: int) -> int:
    return min(max(value, low), high)


def read_settings(session: Session) -> UpgradeSettings:
    days = days_from_mask(system_settings.get_int(session, "agent_upgrade_window_days"))
    start = system_settings.get_int(session, "agent_upgrade_window_start_minutes")
    return UpgradeSettings(
        policy=system_policy(session),
        window=UpgradeWindow(
            days=days,
            start_minutes=start if 0 <= start < 24 * 60 else 120,
            hours=_bounded(
                system_settings.get_int(session, "agent_upgrade_window_hours"),
                WINDOW_HOURS_MIN,
                WINDOW_HOURS_MAX,
            ),
        ),
        failure_limit=_bounded(
            system_settings.get_int(session, "agent_upgrade_failure_limit"),
            FAILURE_LIMIT_MIN,
            FAILURE_LIMIT_MAX,
        ),
        concurrency=_bounded(
            system_settings.get_int(session, "agent_upgrade_concurrency"),
            CONCURRENCY_MIN,
            CONCURRENCY_MAX,
        ),
    )


def set_system_policy(session: Session, policy: str) -> None:
    system_settings.set_choice(session, "agent_upgrade_policy", policy)
    legacy = session.get(SystemSetting, LEGACY_SETTING)
    if legacy is not None:
        session.delete(legacy)


def effective_policy(source_policy: str | None, system: str) -> str:
    """A source's own policy, or the system's when it follows it."""
    if source_policy in SYSTEM_POLICIES:
        return source_policy
    return system if system in SYSTEM_POLICIES else "off"


def is_suspended(failures: int, failure_limit: int) -> bool:
    return failures >= failure_limit


# --- the window --------------------------------------------------------------


def server_timezone() -> tuple[tzinfo, str]:
    """The server's local time zone and its name (TZ, or the system's)."""
    try:
        from tzlocal import get_localzone, get_localzone_name

        zone = get_localzone()
        name = get_localzone_name() or str(zone)
        return zone, name
    except Exception as exc:  # noqa: BLE001 - a broken TZ setting must not stop upgrades
        logger.warning("agent_upgrade.timezone_unknown", error=str(exc))
        return UTC, "UTC"


def _local_start(day, start_minutes: int, zone: tzinfo) -> datetime:
    """The window's start on `day`, as naive UTC like every stored time."""
    local = datetime.combine(day, time(start_minutes // 60, start_minutes % 60), tzinfo=zone)
    return local.astimezone(UTC).replace(tzinfo=None)


def window_bounds(
    window: UpgradeWindow, now: datetime, zone: tzinfo
) -> tuple[datetime, datetime] | None:
    """The window open at `now` (naive UTC) as (start, end) in naive UTC, or
    None. A window belongs to the day it starts on: one that starts on a
    picked day may end the next day, past midnight. The end is exclusive."""
    local_today = now.replace(tzinfo=UTC).astimezone(zone).date()
    for back in (0, 1):
        day = local_today - timedelta(days=back)
        if day.weekday() not in window.days:
            continue
        start = _local_start(day, window.start_minutes, zone)
        end = start + timedelta(hours=window.hours)
        if start <= now < end:
            return start, end
    return None


def next_window_start(window: UpgradeWindow, now: datetime, zone: tzinfo) -> datetime | None:
    """When the next window opens after `now` (naive UTC), or None when no
    day is picked."""
    local_today = now.replace(tzinfo=UTC).astimezone(zone).date()
    for ahead in range(0, 8):
        day = local_today + timedelta(days=ahead)
        if day.weekday() not in window.days:
            continue
        start = _local_start(day, window.start_minutes, zone)
        if start > now:
            return start
    return None


def last_window(session: Session) -> tuple[datetime, datetime] | None:
    row = session.get(SystemSetting, LAST_WINDOW_SETTING)
    if row is None:
        return None
    try:
        data = json.loads(row.value)
        return datetime.fromisoformat(data["start"]), datetime.fromisoformat(data["end"])
    except (ValueError, KeyError, TypeError):
        return None


def remember_window(session: Session, start: datetime, end: datetime) -> bool:
    """Keeps the bounds of the window automatic upgrades run in now, for the
    summary on the Agents page. True when it's a new window."""
    if last_window(session) == (start, end):
        return False
    value = json.dumps({"start": start.isoformat(), "end": end.isoformat()})
    row = session.get(SystemSetting, LAST_WINDOW_SETTING)
    if row is None:
        session.add(SystemSetting(key=LAST_WINDOW_SETTING, value=value))
    else:
        row.value = value
        session.add(row)
    return True
