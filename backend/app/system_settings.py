from sqlmodel import Session

from app.models import SystemSetting

# Missing key == this default, defined here rather than backfilled into
# every existing deployment's DB row-by-row -- see SystemSetting's
# docstring in app/models.py for why.
DEFAULTS: dict[str, str] = {
    "search_view_enabled": "true",
}


def get_bool(session: Session, key: str) -> bool:
    row = session.get(SystemSetting, key)
    raw = row.value if row is not None else DEFAULTS[key]
    return raw == "true"


def get_all_bool(session: Session) -> dict[str, bool]:
    return {key: get_bool(session, key) for key in DEFAULTS}


def set_bool(session: Session, key: str, value: bool) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value="true" if value else "false"))
    else:
        row.value = "true" if value else "false"
