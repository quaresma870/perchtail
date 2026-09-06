from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from app import system_settings
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.db import get_session

router = APIRouter(prefix="/system-settings", tags=["system-settings"])

require_manage = require_global_capability(
    GlobalCapability.manage_system_settings, get_current_active_user
)


class SystemSettingsPublic(BaseModel):
    search_view_enabled: bool
    audit_view_enabled: bool
    audit_retention_days: int


class SystemSettingsUpdate(BaseModel):
    search_view_enabled: bool | None = None
    audit_view_enabled: bool | None = None
    # 0 means "keep forever" -- see system_settings.INT_DEFAULTS's docstring.
    audit_retention_days: int | None = Field(default=None, ge=0)


def _read_all(session: Session) -> SystemSettingsPublic:
    return SystemSettingsPublic(
        **system_settings.get_all_bool(session),
        audit_retention_days=system_settings.get_int(session, "audit_retention_days"),
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
    if "audit_retention_days" in changed:
        system_settings.set_int(session, "audit_retention_days", changed["audit_retention_days"])
    # Iterates BOOL_DEFAULTS itself, not a hardcoded tuple of key names, so a
    # future bool SystemSetting can't be added to BOOL_DEFAULTS/the two
    # Pydantic models above and then silently no-op here because someone
    # forgot to also list it in a third place.
    for key in system_settings.BOOL_DEFAULTS:
        if key in changed:
            system_settings.set_bool(session, key, changed[key])
    record_audit_event(
        session,
        user_id=user.id,
        action="system_settings.update",
        target_type="system_settings",
        metadata=changed,
    )
    session.commit()
    return _read_all(session)
