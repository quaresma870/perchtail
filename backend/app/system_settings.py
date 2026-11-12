from sqlmodel import Session

from app.models import SystemSetting

# Missing key == this default, defined here rather than backfilled into
# every existing deployment's DB row-by-row -- see SystemSetting's
# docstring in app/models.py for why.
BOOL_DEFAULTS: dict[str, str] = {
    "audit_view_enabled": "true",
    # Whether users may switch the viewer's connections between list and
    # tree; when false, everyone sees connections_view_default only (#156).
    "connections_view_switchable": "true",
    # The daily release check (#215); UPDATE_CHECK=false overrides it.
    "update_check_enabled": "true",
    # Password policy (#245, app/password_policy.py). Bandit's B105 takes a
    # "password" key for a hardcoded password; these are setting names.
    "password_reject_simple": "true",  # nosec B105
    "password_reject_personal": "true",  # nosec B105
    # The file-name index (#304, app/name_index.py): names, sizes and times
    # of every source's rule-visible files, never content. On by default.
    "name_index_enabled": "true",
}

# Enumerated string settings: key -> (default, allowed values). A value
# outside the allowed set is never stored (the API validates with a
# Literal, set_choice re-checks).
CHOICE_DEFAULTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "connections_view_default": ("list", ("list", "tree")),
    # How the explorer orders a folder by default (#273); each user can pick
    # their own (app/preferences.py).
    "explorer_sort_default": (
        "name_asc",
        ("name_asc", "name_desc", "mtime_desc", "mtime_asc", "size_desc"),
    ),
    # Upgrading push agents from the portal (#319, app/agent_upgrade_policy.py),
    # Settings -> Agents. Off by default: it replaces a binary on every host
    # that runs an agent. Each host can still refuse with
    # PERCHTAIL_REMOTE_UPGRADE=false.
    "agent_upgrade_policy": ("off", ("off", "manual", "automatic")),
}

# Separate from BOOL_DEFAULTS since get_all_bool()/get_bool() would silently
# misread a non-bool value (int("365") == "true" is just always False, no
# error to catch the mistake) -- keeping the two dicts distinct by type
# makes that a KeyError instead of a wrong answer.
INT_DEFAULTS: dict[str, str] = {
    # Admin-configurable from Settings -> System (see app/audit_purge.py's
    # scheduled sweep), not just an env var -- this was an explicit open
    # decision in ROADMAP.md ("audit log retention -- keep forever, or
    # expire after N months?"), resolved as: admin picks a number, default
    # 365. 0 means "keep forever" (the purge sweep no-ops).
    "audit_retention_days": "365",
    # How many folders one source may read (#161): 1 turns the feature off.
    # Never above source_roots.HARD_MAX_ROOTS, whatever is stored.
    "source_max_roots": "20",
    # Password policy (#245, app/password_policy.py): NIST SP 800-63B
    # defaults, so history, character classes and expiry start off (0).
    # Setting names, not passwords (Bandit B105, as above).
    "password_min_length": "12",  # nosec B105
    "password_max_length": "128",  # nosec B105
    "password_history": "0",  # nosec B105
    "password_required_classes": "0",  # nosec B105
    "password_max_age_days": "0",  # nosec B105
    # How often the explorer re-lists the folders a user has open (#272), in
    # seconds; 0 turns it off. Only the listing, never file content.
    "explorer_refresh_seconds": "30",
    # How often Tail checks an open file for new lines (#254, #283), in
    # seconds: one of TAIL_INTERVALS_SECONDS.
    "tail_interval_seconds": "2",
    # Background check of every source's reachability (#276), in minutes;
    # 0 turns it off. One connection per source per interval.
    "source_check_minutes": "10",
    # How old a source's file-name index may get (#304), in minutes: each
    # source is listed again once its index is older than this.
    "name_index_minutes": "15",
    # Live alerts (#305, app/alerts.py): how often a pass reads what was
    # appended to the files alerts watch.
    "alerts_interval_seconds": "60",
    # Credentials older than this are flagged in the Sources list (#248);
    # 0 turns the warning off.
    "credential_max_age_days": "90",
    # Server-side limits on connector work (#344, app/limits.py): listings,
    # opens, downloads, folder ZIPs and archive listings running at once in
    # all, and on one source; downloads and folder ZIPs one user may have in
    # progress. Over a limit, the request is answered 503 with Retry-After.
    "concurrency_global": "16",
    "concurrency_per_source": "4",
    "concurrency_user_transfers": "2",
    # The most entries a folder or archive listing returns (#347); the
    # explorer says when there were more.
    "listing_max_entries": "5000",
    # Automatic agent upgrades (#319, app/agent_upgrade_policy.py): the
    # window's days (a bit per day, Monday first: 127 is every day), start
    # (minutes after midnight, server time) and length in hours; how many
    # consecutive failures suspend a source's automatic upgrades; and how
    # many agents upgrade at once.
    "agent_upgrade_window_days": "127",
    "agent_upgrade_window_start_minutes": "120",
    "agent_upgrade_window_hours": "4",
    "agent_upgrade_failure_limit": "3",
    "agent_upgrade_concurrency": "5",
}


def get_bool(session: Session, key: str) -> bool:
    row = session.get(SystemSetting, key)
    raw = row.value if row is not None else BOOL_DEFAULTS[key]
    return raw == "true"


def get_all_bool(session: Session) -> dict[str, bool]:
    return {key: get_bool(session, key) for key in BOOL_DEFAULTS}


def set_bool(session: Session, key: str, value: bool) -> None:
    if key not in BOOL_DEFAULTS:
        raise KeyError(key)
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value="true" if value else "false"))
    else:
        row.value = "true" if value else "false"


def get_int(session: Session, key: str) -> int:
    row = session.get(SystemSetting, key)
    raw = row.value if row is not None else INT_DEFAULTS[key]
    return int(raw)


def set_int(session: Session, key: str, value: int) -> None:
    if key not in INT_DEFAULTS:
        raise KeyError(key)
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=str(value)))
    else:
        row.value = str(value)


def get_choice(session: Session, key: str) -> str:
    default, allowed = CHOICE_DEFAULTS[key]
    row = session.get(SystemSetting, key)
    value = row.value if row is not None else default
    return value if value in allowed else default


def set_choice(session: Session, key: str, value: str) -> None:
    _default, allowed = CHOICE_DEFAULTS[key]
    if value not in allowed:
        raise ValueError(f"{key} must be one of {allowed}")
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
