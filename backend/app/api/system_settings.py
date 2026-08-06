from fastapi import APIRouter, Depends
from pydantic import BaseModel
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


class SystemSettingsUpdate(BaseModel):
    search_view_enabled: bool | None = None


@router.get("", response_model=SystemSettingsPublic)
def get_system_settings(
    # Readable by any authenticated user, not just admins -- the frontend
    # needs this to decide whether to render the Search nav link at all,
    # for everyone, not only for whoever can change it.
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    return SystemSettingsPublic(**system_settings.get_all_bool(session))


@router.patch("", response_model=SystemSettingsPublic)
def update_system_settings(
    payload: SystemSettingsUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    changed = payload.model_dump(exclude_unset=True)
    for key, value in changed.items():
        system_settings.set_bool(session, key, value)
    record_audit_event(
        session,
        user_id=user.id,
        action="system_settings.update",
        target_type="system_settings",
        metadata=changed,
    )
    session.commit()
    return SystemSettingsPublic(**system_settings.get_all_bool(session))
