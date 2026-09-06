from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import Capability, GlobalCapability, User
from app.auth.rbac import require_capability, require_global_capability
from app.db import get_session
from app.models import PatternKind, SeverityLevel, SeverityPattern, Source
from app.rules import parse_pattern
from app.severity_patterns import effective_patterns

require_manage_global = require_global_capability(
    GlobalCapability.manage_system_settings, get_current_active_user
)
require_view_source = require_capability(Capability.view, get_current_active_user)
require_manage_source = require_capability(Capability.manage_rules, get_current_active_user)


class SeverityPatternPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int | None
    level: SeverityLevel
    pattern: str
    pattern_kind: PatternKind
    enabled: bool
    highlight_line: bool
    include_in_navigation: bool


class SeverityPatternCreate(BaseModel):
    level: SeverityLevel
    # Same admin-facing syntax as Rule: default glob, `re:` prefix for regex.
    pattern: str
    enabled: bool = True
    highlight_line: bool = False
    include_in_navigation: bool = True


class SeverityPatternUpdate(BaseModel):
    level: SeverityLevel | None = None
    pattern: str | None = None
    enabled: bool | None = None
    highlight_line: bool | None = None
    include_in_navigation: bool | None = None


def _apply_update(pattern: SeverityPattern, payload: SeverityPatternUpdate) -> None:
    if payload.level is not None:
        pattern.level = payload.level
    if payload.pattern is not None:
        pattern.pattern, pattern.pattern_kind = parse_pattern(payload.pattern)
    if payload.enabled is not None:
        pattern.enabled = payload.enabled
    if payload.highlight_line is not None:
        pattern.highlight_line = payload.highlight_line
    if payload.include_in_navigation is not None:
        pattern.include_in_navigation = payload.include_in_navigation


# --- Global default set -----------------------------------------------------

global_router = APIRouter(prefix="/severity-patterns", tags=["severity-patterns"])


@global_router.get("", response_model=list[SeverityPatternPublic])
def list_global_patterns(
    user: User = Depends(require_manage_global), session: Session = Depends(get_session)
):
    return session.exec(select(SeverityPattern).where(SeverityPattern.source_id.is_(None))).all()


@global_router.post("", response_model=SeverityPatternPublic, status_code=status.HTTP_201_CREATED)
def create_global_pattern(
    payload: SeverityPatternCreate,
    user: User = Depends(require_manage_global),
    session: Session = Depends(get_session),
):
    pattern_value, pattern_kind = parse_pattern(payload.pattern)
    pattern = SeverityPattern(
        source_id=None,
        level=payload.level,
        pattern=pattern_value,
        pattern_kind=pattern_kind,
        enabled=payload.enabled,
        highlight_line=payload.highlight_line,
        include_in_navigation=payload.include_in_navigation,
    )
    session.add(pattern)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="severity_pattern.create",
        target_type="severity_pattern",
        target_id=pattern.id,
        metadata={"scope": "global", "level": payload.level},
    )
    session.commit()
    session.refresh(pattern)
    return pattern


@global_router.patch("/{pattern_id}", response_model=SeverityPatternPublic)
def update_global_pattern(
    pattern_id: int,
    payload: SeverityPatternUpdate,
    user: User = Depends(require_manage_global),
    session: Session = Depends(get_session),
):
    pattern = session.get(SeverityPattern, pattern_id)
    if pattern is None or pattern.source_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pattern not found")

    _apply_update(pattern, payload)
    session.add(pattern)
    record_audit_event(
        session,
        user_id=user.id,
        action="severity_pattern.update",
        target_type="severity_pattern",
        target_id=pattern.id,
        metadata={"scope": "global"},
    )
    session.commit()
    session.refresh(pattern)
    return pattern


@global_router.delete("/{pattern_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_global_pattern(
    pattern_id: int,
    user: User = Depends(require_manage_global),
    session: Session = Depends(get_session),
):
    pattern = session.get(SeverityPattern, pattern_id)
    if pattern is None or pattern.source_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pattern not found")

    session.delete(pattern)
    record_audit_event(
        session,
        user_id=user.id,
        action="severity_pattern.delete",
        target_type="severity_pattern",
        target_id=pattern_id,
        metadata={"scope": "global"},
    )
    session.commit()


# --- Per-source overrides ----------------------------------------------------

source_router = APIRouter(
    prefix="/sources/{source_id}/severity-patterns", tags=["severity-patterns"]
)


@source_router.get("/effective", response_model=list[SeverityPatternPublic])
def get_effective_patterns(
    source: Source = Depends(require_view_source), session: Session = Depends(get_session)
):
    return effective_patterns(session, source.id)


@source_router.get("", response_model=list[SeverityPatternPublic])
def list_source_patterns(
    source: Source = Depends(require_manage_source), session: Session = Depends(get_session)
):
    return session.exec(select(SeverityPattern).where(SeverityPattern.source_id == source.id)).all()


@source_router.post("", response_model=SeverityPatternPublic, status_code=status.HTTP_201_CREATED)
def create_source_pattern(
    payload: SeverityPatternCreate,
    source: Source = Depends(require_manage_source),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    pattern_value, pattern_kind = parse_pattern(payload.pattern)
    pattern = SeverityPattern(
        source_id=source.id,
        level=payload.level,
        pattern=pattern_value,
        pattern_kind=pattern_kind,
        enabled=payload.enabled,
        highlight_line=payload.highlight_line,
        include_in_navigation=payload.include_in_navigation,
    )
    session.add(pattern)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="severity_pattern.create",
        target_type="severity_pattern",
        target_id=pattern.id,
        metadata={"scope": "source", "source_id": source.id, "level": payload.level},
    )
    session.commit()
    session.refresh(pattern)
    return pattern


@source_router.patch("/{pattern_id}", response_model=SeverityPatternPublic)
def update_source_pattern(
    pattern_id: int,
    payload: SeverityPatternUpdate,
    source: Source = Depends(require_manage_source),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    pattern = session.get(SeverityPattern, pattern_id)
    if pattern is None or pattern.source_id != source.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pattern not found")

    _apply_update(pattern, payload)
    session.add(pattern)
    record_audit_event(
        session,
        user_id=user.id,
        action="severity_pattern.update",
        target_type="severity_pattern",
        target_id=pattern.id,
        metadata={"scope": "source", "source_id": source.id},
    )
    session.commit()
    session.refresh(pattern)
    return pattern


@source_router.delete("/{pattern_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source_pattern(
    pattern_id: int,
    source: Source = Depends(require_manage_source),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    pattern = session.get(SeverityPattern, pattern_id)
    if pattern is None or pattern.source_id != source.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pattern not found")

    session.delete(pattern)
    record_audit_event(
        session,
        user_id=user.id,
        action="severity_pattern.delete",
        target_type="severity_pattern",
        target_id=pattern_id,
        metadata={"scope": "source", "source_id": source.id},
    )
    session.commit()
