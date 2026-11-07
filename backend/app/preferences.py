"""Per-user preferences (#273): small display choices that follow a user
across browsers and machines, each falling back to a deployment-wide
default under Settings -> System when the user hasn't chosen.

Stored as a JSON object on the user (User.preferences); only the keys
and values listed here are ever accepted or returned."""

from sqlmodel import Session

from app import system_settings
from app.auth.models import User

EXPLORER_SORTS = ("name_asc", "name_desc", "mtime_desc", "mtime_asc", "size_desc")
CONNECTIONS_VIEWS = ("list", "tree")

# Preference key -> (allowed values, the System setting with its default).
CHOICES: dict[str, tuple[tuple[str, ...], str]] = {
    "explorer_sort": (EXPLORER_SORTS, "explorer_sort_default"),
    "connections_view": (CONNECTIONS_VIEWS, "connections_view_default"),
}


def own_choices(user: User) -> dict[str, str]:
    """What the user chose, ignoring anything stored that isn't (or is no
    longer) a valid choice."""
    stored = user.preferences if isinstance(user.preferences, dict) else {}
    return {
        key: value for key, value in stored.items() if key in CHOICES and value in CHOICES[key][0]
    }


def effective(session: Session, user: User) -> dict[str, str]:
    """Each preference: the user's choice, or the System default."""
    chosen = own_choices(user)
    return {
        key: chosen.get(key) or system_settings.get_choice(session, default_key)
        for key, (_allowed, default_key) in CHOICES.items()
    }


def update(user: User, changes: dict[str, str | None]) -> None:
    """Sets (or, with None, clears back to the default) the given keys.
    Raises ValueError for an unknown key or value. Doesn't commit."""
    chosen = own_choices(user)
    for key, value in changes.items():
        if key not in CHOICES:
            raise ValueError(f"unknown preference: {key}")
        if value is None:
            chosen.pop(key, None)
        elif value in CHOICES[key][0]:
            chosen[key] = value
        else:
            raise ValueError(f"{key} must be one of {', '.join(CHOICES[key][0])}")
    # A new object, so SQLAlchemy sees the JSON column change.
    user.preferences = dict(chosen) or None
