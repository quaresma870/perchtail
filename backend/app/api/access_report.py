"""The access report (#384, app/access_report.py): who holds what, where,
as JSON for the Access review page or as a CSV download. Needs the
`view_access_report` global capability; a delegated admin's report covers
only their own scope. Every report generated is audited, without its
content."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import Session

from app import access_report
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.db import get_session

router = APIRouter(prefix="/access-report", tags=["access-report"])

require_view = require_global_capability(
    GlobalCapability.view_access_report, get_current_active_user
)


@router.get("")
def get_access_report(
    view: Literal["users", "sources"] = access_report.VIEW_USERS,
    report_format: Literal["json", "csv"] = Query("json", alias="format"),
    inactive_days: int = Query(
        access_report.DEFAULT_INACTIVE_DAYS,
        ge=access_report.MIN_INACTIVE_DAYS,
        le=access_report.MAX_INACTIVE_DAYS,
    ),
    user: User = Depends(require_view),
    session: Session = Depends(get_session),
):
    """The access report, by user or by source. `format=csv` downloads it
    as a file."""
    report = access_report.build_report(session, user, inactive_days=inactive_days)
    record_audit_event(
        session,
        user_id=user.id,
        action="access_report.generate",
        target_type="access_report",
        metadata={
            "view": view,
            "format": report_format,
            "scope": report.scope,
            "inactive_days": inactive_days,
            "users": len(report.users),
            "rows": report.row_count,
        },
    )
    session.commit()

    if report_format == "json":
        return JSONResponse(access_report.to_json(report, view))

    filename = f"perchtail-access-report-{view}-{report.generated_at.strftime('%Y-%m-%d')}.csv"
    return StreamingResponse(
        access_report.iter_csv(report, view),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
