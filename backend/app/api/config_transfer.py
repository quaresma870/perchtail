"""Bulk import/export of the source configuration as YAML (#179); the
format and the matching rules are in app/config_transfer.py and
docs/bulk-import.md. Super admins only: one file can create or repoint any
number of sources, and an import may carry credentials."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from app import config_transfer
from app.api.auth import require_super_admin
from app.audit import record_audit_event
from app.auth.models import User
from app.db import get_session
from app.logging_config import get_logger

router = APIRouter(prefix="/admin/config", tags=["config"])
logger = get_logger(__name__)


class ImportRequest(BaseModel):
    yaml: str = Field(max_length=config_transfer.MAX_BYTES)
    dry_run: bool = True
    # Credentials typed in the import screen, by the source key the plan
    # shows ("Customer / folder / source"). They win over the file's own.
    credentials: dict[str, dict[str, str]] = {}


class PlanItemPublic(BaseModel):
    action: str
    kind: str
    key: str
    changes: list[str]
    needs_credential: bool
    protocol: str | None = None


class ProblemPublic(BaseModel):
    where: str
    message: str


class ImportResult(BaseModel):
    applied: bool
    items: list[PlanItemPublic]
    problems: list[ProblemPublic]
    # Not blocking: the file still applies, but should be changed.
    warnings: list[ProblemPublic] = []


@router.post("/import", response_model=ImportResult)
def import_config(
    payload: ImportRequest,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Checks the file and returns what it would change (dry_run, the
    default), or applies it in one transaction. Nothing is written if any
    problem is found."""
    for key, credential in payload.credentials.items():
        if set(credential) - {"username", "password", "private_key"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Credential for {key!r} takes username plus password or private_key",
            )
    credentials = {k: {f: v for f, v in c.items() if v} for k, c in payload.credentials.items()}
    credentials = {k: c for k, c in credentials.items() if c.get("username")}

    parsed = config_transfer.parse(payload.yaml)
    apply = not payload.dry_run
    try:
        items, problems = config_transfer.run(
            session, parsed, credentials=credentials, apply=apply, user_id=user.id
        )
        if apply and problems:
            session.rollback()
        elif apply:
            counts = {
                action: sum(1 for i in items if i.action == action)
                for action in ("create", "update", "unchanged")
            }
            record_audit_event(
                session,
                user_id=user.id,
                action="config.import",
                metadata={"sources": len(parsed.sources), **counts},
            )
            session.commit()
    except Exception:
        session.rollback()
        logger.exception("config.import.failed")
        raise

    return ImportResult(
        applied=apply and not problems,
        items=[PlanItemPublic(**asdict(i)) for i in items],
        problems=[ProblemPublic(**asdict(p)) for p in problems],
        warnings=[ProblemPublic(**asdict(w)) for w in parsed.warnings],
    )


@router.get("/export", response_class=PlainTextResponse)
def export_config(
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Every customer, folder, source and rule as an importable YAML file,
    without credentials."""
    text = config_transfer.export(session)
    record_audit_event(session, user_id=user.id, action="config.export")
    session.commit()
    return PlainTextResponse(
        text,
        media_type="application/yaml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="perchtail-config.yaml"'},
    )
