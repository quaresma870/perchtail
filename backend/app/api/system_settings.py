from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app import password_policy, system_settings
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.db import get_session
from app.source_roots import HARD_MAX_ROOTS, max_roots

router = APIRouter(prefix="/system-settings", tags=["system-settings"])

require_manage = require_global_capability(
    GlobalCapability.manage_system_settings, get_current_active_user
)


ExplorerSort = Literal["name_asc", "name_desc", "mtime_desc", "mtime_asc", "size_desc"]
# How often Tail polls an open file (#283): a System setting, not each
# user's, so sources see a known load.
TailIntervalSeconds = Literal[1, 2, 5, 10]


class SystemSettingsPublic(BaseModel):
    search_view_enabled: bool
    audit_view_enabled: bool
    audit_retention_days: int
    source_max_roots: int
    connections_view_switchable: bool
    connections_view_default: Literal["list", "tree"]
    update_check_enabled: bool
    # Password policy (#245, app/password_policy.py).
    password_min_length: int
    password_max_length: int
    password_reject_simple: bool
    password_reject_personal: bool
    password_history: int
    password_required_classes: int
    password_max_age_days: int
    # Explorer (#272, #273).
    explorer_sort_default: ExplorerSort
    explorer_refresh_seconds: int
    tail_interval_seconds: int
    # Background source check (#276), minutes; 0 = off.
    source_check_minutes: int
    # File-name index (#304).
    name_index_enabled: bool
    name_index_minutes: int


class SystemSettingsUpdate(BaseModel):
    search_view_enabled: bool | None = None
    audit_view_enabled: bool | None = None
    # 0 means "keep forever" -- see system_settings.INT_DEFAULTS's docstring.
    audit_retention_days: int | None = Field(default=None, ge=0)
    # Folders per source (#161): 1 turns several folders off; 100 is the
    # hard cap (source_roots.HARD_MAX_ROOTS).
    source_max_roots: int | None = Field(default=None, ge=1, le=HARD_MAX_ROOTS)
    connections_view_switchable: bool | None = None
    connections_view_default: Literal["list", "tree"] | None = None
    update_check_enabled: bool | None = None
    password_min_length: int | None = Field(
        default=None, ge=password_policy.MIN_LENGTH_FLOOR, le=password_policy.MAX_LENGTH_CEILING
    )
    password_max_length: int | None = Field(
        default=None, ge=password_policy.MAX_LENGTH_FLOOR, le=password_policy.MAX_LENGTH_CEILING
    )
    password_reject_simple: bool | None = None
    password_reject_personal: bool | None = None
    # 0 = off; otherwise the current password and the N-1 before it.
    password_history: int | None = Field(default=None, ge=0, le=password_policy.MAX_HISTORY)
    # How many of lowercase, uppercase, digit, symbol; 0 = off.
    password_required_classes: int | None = Field(default=None, ge=0, le=4)
    # 0 = passwords don't expire.
    password_max_age_days: int | None = Field(
        default=None, ge=0, le=password_policy.MAX_AGE_DAYS_CEILING
    )
    explorer_sort_default: ExplorerSort | None = None
    # 0 = off; otherwise at least 5 s, so open folders can't hammer sources.
    explorer_refresh_seconds: int | None = Field(default=None, ge=0, le=3600)
    tail_interval_seconds: TailIntervalSeconds | None = None
    source_check_minutes: int | None = Field(default=None, ge=0, le=1440)
    name_index_enabled: bool | None = None
    # At least 5 minutes, so listings can't hammer the sources; at most a day.
    name_index_minutes: int | None = Field(default=None, ge=5, le=1440)


_PASSWORD_INTS = (
    "password_min_length",
    "password_max_length",
    "password_history",
    "password_required_classes",
    "password_max_age_days",
)


def _read_all(session: Session) -> SystemSettingsPublic:
    return SystemSettingsPublic(
        **system_settings.get_all_bool(session),
        audit_retention_days=system_settings.get_int(session, "audit_retention_days"),
        source_max_roots=max_roots(session),
        connections_view_default=system_settings.get_choice(session, "connections_view_default"),
        explorer_sort_default=system_settings.get_choice(session, "explorer_sort_default"),
        explorer_refresh_seconds=system_settings.get_int(session, "explorer_refresh_seconds"),
        tail_interval_seconds=system_settings.get_int(session, "tail_interval_seconds"),
        source_check_minutes=system_settings.get_int(session, "source_check_minutes"),
        name_index_minutes=system_settings.get_int(session, "name_index_minutes"),
        **{key: system_settings.get_int(session, key) for key in _PASSWORD_INTS},
    )


@router.get("", response_model=SystemSettingsPublic)
def get_system_settings(
    # Readable by any authenticated user, not just admins -- the frontend
    # needs this to decide whether to render the Search nav link at all,
    # for everyone, not only for whoever can change it.
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    return _read_all(session)


@router.patch("", response_model=SystemSettingsPublic)
def update_system_settings(
    payload: SystemSettingsUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    # exclude_unset still lets an explicit `null` through (it was "set", just
    # to nothing) -- filter those out rather than passing None on to
    # set_int/set_bool, which would stringify it into a value the getters
    # can't parse back (int("None") raises) on every read after this.
    changed = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    min_length = changed.get(
        "password_min_length", system_settings.get_int(session, "password_min_length")
    )
    max_length = changed.get(
        "password_max_length", system_settings.get_int(session, "password_max_length")
    )
    if 0 < changed.get("explorer_refresh_seconds", 0) < 5:
        raise HTTPException(
            status_code=422, detail="Refresh open folders every 5 seconds at least, or 0 for off"
        )
    if min_length > max_length:
        raise HTTPException(
            status_code=422,
            detail="The minimum password length can't be above the maximum",
        )
    for key in system_settings.INT_DEFAULTS:
        if key in changed:
            system_settings.set_int(session, key, changed[key])
    # Iterates BOOL_DEFAULTS itself, not a hardcoded tuple of key names, so a
    # future bool SystemSetting can't be added to BOOL_DEFAULTS/the two
    # Pydantic models above and then silently no-op here because someone
    # forgot to also list it in a third place.
    for key in system_settings.BOOL_DEFAULTS:
        if key in changed:
            system_settings.set_bool(session, key, changed[key])
    for key in system_settings.CHOICE_DEFAULTS:
        if key in changed:
            system_settings.set_choice(session, key, changed[key])
    # Lowering or turning off "don't reuse" drops the hashes it no longer
    # needs right away, not at each account's next change (#245).
    if "password_history" in changed:
        password_policy.trim_all_history(session, changed["password_history"])
    record_audit_event(
        session,
        user_id=user.id,
        action="system_settings.update",
        target_type="system_settings",
        metadata=changed,
    )
    session.commit()
    return _read_all(session)
