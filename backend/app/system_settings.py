from sqlmodel import Session

from app.models import SystemSetting

# Missing key == this default, defined here rather than backfilled into
# every existing deployment's DB row-by-row -- see SystemSetting's
# docstring in app/models.py for why.
BOOL_DEFAULTS: dict[str, str] = {
    "search_view_enabled": "true",
    "audit_view_enabled": "true",
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
