from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from app.alerts import send_webhook
from app.api.auth import get_current_active_user
from app.auth.models import Capability, User
from app.auth.rbac import visible_source_ids
from app.db import get_session
from app.models import Alert
from app.search_index import SearchHit
from app.timeutils import utcnow

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertPublic(BaseModel):
    id: int
    name: str
    query: str
    source_id: int | None
    webhook_url: str
    enabled: bool
    last_checked_at: str | None

    @classmethod
    def from_model(cls, alert: Alert) -> "AlertPublic":
        return cls(
            id=alert.id,
            name=alert.name,
            query=alert.query,
            source_id=alert.source_id,
            webhook_url=alert.webhook_url,
            enabled=alert.enabled,
            last_checked_at=alert.last_checked_at.isoformat() if alert.last_checked_at else None,
        )


class AlertCreate(BaseModel):
    name: str
    query: str
    source_id: int | None = None
    webhook_url: str
    enabled: bool = True

    @field_validator("webhook_url")
    @classmethod
    def _webhook_url_is_http(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("webhook_url must be an http:// or https:// URL")
        return v


class AlertUpdate(BaseModel):
    name: str | None = None
    query: str | None = None
    source_id: int | None = None
    webhook_url: str | None = None
    enabled: bool | None = None

    @field_validator("webhook_url")
    @classmethod
    def _webhook_url_is_http(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("webhook_url must be an http:// or https:// URL")
        return v


def _require_visible_source(session: Session, user: User, source_id: int | None) -> None:
    """An alert can only be scoped to a source the owner can currently
    view -- same check evaluate_alerts() re-runs on every sweep (see
    app.alerts), just enforced up front too so a user can't create an
    alert against a source they have no business pointing it at."""
    if source_id is None:
        return
    visible = visible_source_ids(session, user, Capability.view)
    if visible is not None and source_id not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted for this source"
        )


@router.get("", response_model=list[AlertPublic])
def list_alerts(
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    alerts = session.exec(select(Alert).where(Alert.user_id == user.id)).all()
    return [AlertPublic.from_model(a) for a in alerts]


@router.post("", response_model=AlertPublic, status_code=status.HTTP_201_CREATED)
def create_alert(
    payload: AlertCreate,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    _require_visible_source(session, user, payload.source_id)
    alert = Alert(
        user_id=user.id,
        name=payload.name,
        query=payload.query,
        source_id=payload.source_id,
        webhook_url=payload.webhook_url,
        enabled=payload.enabled,
        created_at=utcnow(),
    )
    session.add(alert)
    session.commit()
    session.refresh(alert)
    return AlertPublic.from_model(alert)


def _get_owned_alert(session: Session, user: User, alert_id: int) -> Alert:
    alert = session.get(Alert, alert_id)
    if alert is None or alert.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    return alert


@router.patch("/{alert_id}", response_model=AlertPublic)
def update_alert(
    alert_id: int,
    payload: AlertUpdate,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    alert = _get_owned_alert(session, user, alert_id)
    changed = payload.model_dump(exclude_unset=True)
    if "source_id" in changed:
        _require_visible_source(session, user, changed["source_id"])
    for key, value in changed.items():
        setattr(alert, key, value)
    session.add(alert)
    session.commit()
    session.refresh(alert)
    return AlertPublic.from_model(alert)


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert(
    alert_id: int,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    alert = _get_owned_alert(session, user, alert_id)
    session.delete(alert)
    session.commit()


class AlertTestResult(BaseModel):
    ok: bool


@router.post("/{alert_id}/test", response_model=AlertTestResult)
def test_alert_webhook(
    alert_id: int,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    """Sends a single synthetic hit to the alert's webhook so the owner can
    verify the URL/receiver actually works, without waiting for real
    matching content to show up in the index."""
    alert = _get_owned_alert(session, user, alert_id)
    sample_hit = SearchHit(
        source_id=alert.source_id or 0,
        file_path="test.log",
        line_number=1,
        snippet_html=f"This is a test notification for alert &quot;{alert.name}&quot;.",
        matched_field="content",
    )
    ok = send_webhook(alert, [sample_hit])
    return AlertTestResult(ok=ok)
