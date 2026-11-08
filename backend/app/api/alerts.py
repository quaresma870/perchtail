from urllib.parse import urlparse

import re2
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlmodel import Session, select

from app.alerts import compile_query, forget_alert, post_webhook
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import Capability, GlobalCapability, User
from app.auth.rbac import require_global_capability, visible_source_ids
from app.db import get_session
from app.models import Alert, Customer, Folder, Source
from app.timeutils import utcnow
from app.webhook_safety import UnsafeWebhookURLError, assert_webhook_url_is_safe

require_manage_alerts = require_global_capability(
    GlobalCapability.manage_alerts, get_current_active_user
)

router = APIRouter(prefix="/alerts", tags=["alerts"])

_SCOPE_FIELDS = ("source_id", "folder_id", "customer_id")


class AlertPublic(BaseModel):
    id: int
    name: str
    query: str
    regex: bool
    source_id: int | None
    folder_id: int | None
    customer_id: int | None
    file_glob: str
    webhook_url: str
    enabled: bool
    last_checked_at: str | None
    last_fired_at: str | None
    failed_deliveries: int
    last_error: str | None

    @classmethod
    def from_model(cls, alert: Alert) -> "AlertPublic":
        return cls(
            id=alert.id,
            name=alert.name,
            query=alert.query,
            regex=alert.regex,
            source_id=alert.source_id,
            folder_id=alert.folder_id,
            customer_id=alert.customer_id,
            file_glob=alert.file_glob,
            webhook_url=alert.webhook_url,
            enabled=alert.enabled,
            last_checked_at=alert.last_checked_at.isoformat() if alert.last_checked_at else None,
            last_fired_at=alert.last_fired_at.isoformat() if alert.last_fired_at else None,
            failed_deliveries=alert.failed_deliveries,
            last_error=alert.last_error,
        )


def _check_webhook_url(v: str) -> str:
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("webhook_url must be an http:// or https:// URL")
    try:
        assert_webhook_url_is_safe(v)
    except UnsafeWebhookURLError as exc:
        raise ValueError(str(exc)) from exc
    return v


def _check_scope(values: dict) -> None:
    if sum(1 for key in _SCOPE_FIELDS if values.get(key) is not None) > 1:
        raise ValueError("an alert is scoped to one source, folder or customer at most")


class AlertCreate(BaseModel):
    name: str
    query: str
    regex: bool = False
    source_id: int | None = None
    folder_id: int | None = None
    customer_id: int | None = None
    file_glob: str = "**/*.log"
    webhook_url: str
    enabled: bool = True

    @field_validator("webhook_url")
    @classmethod
    def _webhook_url_is_http(cls, v: str) -> str:
        return _check_webhook_url(v)

    @model_validator(mode="after")
    def _one_scope(self) -> "AlertCreate":
        _check_scope(self.model_dump())
        if not self.query.strip():
            raise ValueError("query must not be empty")
        if not self.file_glob.strip():
            raise ValueError("file_glob must not be empty")
        return self


class AlertUpdate(BaseModel):
    name: str | None = None
    query: str | None = None
    regex: bool | None = None
    source_id: int | None = None
    folder_id: int | None = None
    customer_id: int | None = None
    file_glob: str | None = None
    webhook_url: str | None = None
    enabled: bool | None = None

    @field_validator("webhook_url")
    @classmethod
    def _webhook_url_is_http(cls, v: str | None) -> str | None:
        return v if v is None else _check_webhook_url(v)

    @model_validator(mode="after")
    def _one_scope(self) -> "AlertUpdate":
        _check_scope(self.model_dump())
        return self


def _require_valid_query(query: str, regex: bool) -> None:
    try:
        compile_query(query, regex)
    except re2.error as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid regex (alerts use RE2: no backreferences or lookaround): {exc}",
        ) from exc


def _require_scope_allowed(session: Session, user: User, alert: Alert) -> None:
    """An alert can only point at a source, folder or customer that exists
    and that the owner can reach -- checked again at every pass (see
    app.alerts), and up front here so nobody sets one up on something they
    have no business watching."""
    visible = visible_source_ids(session, user, Capability.view)
    if alert.source_id is not None:
        if session.get(Source, alert.source_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
        if visible is not None and alert.source_id not in visible:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted for this source"
            )
    for key, model, label in (
        ("folder_id", Folder, "Folder"),
        ("customer_id", Customer, "Customer"),
    ):
        scope_id = getattr(alert, key)
        if scope_id is None:
            continue
        if session.get(model, scope_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
        if visible is not None:
            column = Source.folder_id if key == "folder_id" else Source.customer_id
            reachable = session.exec(
                select(Source.id).where(column == scope_id, Source.id.in_(visible))
            ).first()
            if reachable is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Not permitted for this {label.lower()}",
                )


@router.get("", response_model=list[AlertPublic])
def list_alerts(
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    alerts = session.exec(select(Alert).where(Alert.user_id == user.id)).all()
    return [AlertPublic.from_model(a) for a in alerts]


def _webhook_host(url: str) -> str | None:
    # Only the host goes into the audit log: the path or query string of a
    # webhook URL often carries a secret token.
    return urlparse(url).hostname


def _scope_metadata(alert: Alert) -> dict:
    return {key: getattr(alert, key) for key in _SCOPE_FIELDS if getattr(alert, key) is not None}


@router.post("", response_model=AlertPublic, status_code=status.HTTP_201_CREATED)
def create_alert(
    payload: AlertCreate,
    user: User = Depends(require_manage_alerts),
    session: Session = Depends(get_session),
):
    _require_valid_query(payload.query, payload.regex)
    alert = Alert(user_id=user.id, created_at=utcnow(), **payload.model_dump())
    _require_scope_allowed(session, user, alert)
    session.add(alert)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="alert.create",
        target_type="alert",
        target_id=alert.id,
        metadata={
            "name": alert.name,
            **_scope_metadata(alert),
            "file_glob": alert.file_glob,
            "webhook_host": _webhook_host(alert.webhook_url),
        },
    )
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
    user: User = Depends(require_manage_alerts),
    session: Session = Depends(get_session),
):
    alert = _get_owned_alert(session, user, alert_id)
    changed = payload.model_dump(exclude_unset=True)
    if any(key in changed for key in _SCOPE_FIELDS):
        # A new scope replaces the old one whole.
        for key in _SCOPE_FIELDS:
            setattr(alert, key, changed.pop(key, None))
        changed["scope"] = _scope_metadata(alert)
    for key, value in changed.items():
        if key != "scope":
            setattr(alert, key, value)
    _require_valid_query(alert.query, alert.regex)
    _require_scope_allowed(session, user, alert)
    if not alert.query.strip() or not alert.file_glob.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="query and file_glob must not be empty",
        )
    session.add(alert)
    metadata: dict = {"fields": sorted(changed)}
    if "webhook_url" in changed:
        metadata["webhook_host"] = _webhook_host(alert.webhook_url)
    record_audit_event(
        session,
        user_id=user.id,
        action="alert.update",
        target_type="alert",
        target_id=alert.id,
        metadata=metadata,
    )
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
    # Nothing of an alert may outlive it: its file positions and pending
    # deliveries go with it.
    forget_alert(session, alert.id)
    record_audit_event(
        session,
        user_id=user.id,
        action="alert.delete",
        target_type="alert",
        target_id=alert.id,
        metadata={"name": alert.name},
    )
    session.delete(alert)
    session.commit()


class AlertTestResult(BaseModel):
    ok: bool
    error: str | None = None


@router.post("/{alert_id}/test", response_model=AlertTestResult)
def test_alert_webhook(
    alert_id: int,
    user: User = Depends(require_manage_alerts),
    session: Session = Depends(get_session),
):
    """Sends one sample call to the alert's webhook, in the same shape as a
    real one, so the owner can check the receiver without waiting for a
    matching line."""
    alert = _get_owned_alert(session, user, alert_id)
    error = post_webhook(
        alert.webhook_url,
        {
            "alert_id": alert.id,
            "alert_name": alert.name,
            "query": alert.query,
            "regex": alert.regex,
            "matched_count": 1,
            "test": True,
            "hits": [
                {
                    "source_id": alert.source_id,
                    "source_name": "test",
                    "file_path": "test.log",
                    "offset": 0,
                    "line": f'This is a test notification for alert "{alert.name}".',
                }
            ],
        },
    )
    return AlertTestResult(ok=error is None, error=error)
