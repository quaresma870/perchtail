"""The Updates card under Settings -> System (#215): the running version,
how it was installed, the latest release, and a "Check now". Super admins
only: install details and upgrade steps are an operator concern."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app import update_check
from app.api.auth import require_super_admin
from app.auth.models import User
from app.db import get_session
from app.version import APP_VERSION

router = APIRouter(prefix="/system/update", tags=["system"])


class UpdateStatus(BaseModel):
    enabled: bool
    # True when UPDATE_CHECK=false: the switch under System can't turn it on.
    disabled_by_env: bool
    running_version: str
    install_method: str
    latest_version: str | None
    latest_name: str | None
    latest_url: str | None
    latest_published_at: datetime | None
    update_available: bool
    last_checked_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None


def _status(session: Session) -> UpdateStatus:
    state = update_check.get_state(session)
    return UpdateStatus(
        enabled=update_check.is_enabled(session),
        disabled_by_env=not update_check.enabled_by_env(),
        running_version=APP_VERSION,
        install_method=update_check.install_method(),
        latest_version=state.latest_version,
        latest_name=state.latest_name,
        latest_url=state.latest_url,
        latest_published_at=state.latest_published_at,
        update_available=update_check.update_available(state),
        last_checked_at=state.last_checked_at,
        last_success_at=state.last_success_at,
        last_error=state.last_error,
    )


@router.get("", response_model=UpdateStatus)
def get_update_status(
    user: User = Depends(require_super_admin), session: Session = Depends(get_session)
):
    status_ = _status(session)
    session.commit()  # get_state may have created the singleton row
    return status_


@router.post("/check", response_model=UpdateStatus)
def check_for_updates(
    user: User = Depends(require_super_admin), session: Session = Depends(get_session)
):
    """Checks now, whatever the time since the last check. Refused while
    the check is off, so it never makes a request the admin turned off."""
    if not update_check.is_enabled(session):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="The release check is turned off"
        )
    update_check.check_now(session)
    session.commit()
    return _status(session)
