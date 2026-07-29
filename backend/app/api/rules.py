from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import Capability, User
from app.auth.rbac import require_capability
from app.db import get_session
from app.models import PatternKind, Rule, RuleType, Source
from app.rules import parse_pattern

router = APIRouter(prefix="/sources/{source_id}/rules", tags=["rules"])

require_view = require_capability(Capability.view, get_current_active_user)
require_manage_rules = require_capability(Capability.manage_rules, get_current_active_user)


class RulePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order: int
    type: RuleType
    pattern: str
    pattern_kind: PatternKind
    notes: str | None


class RuleCreate(BaseModel):
    type: RuleType
    # Admin-facing syntax: default glob, prefix with `re:` for regex (see
    # CLAUDE.md's "Rule matching semantics").
    pattern: str
    notes: str | None = None


class RuleUpdate(BaseModel):
    type: RuleType | None = None
    pattern: str | None = None
    notes: str | None = None


class ReorderRequest(BaseModel):
    rule_ids: list[int]


class RawRulesRequest(BaseModel):
    text: str


def _require_editable_rules(source: Source) -> None:
    # Per CLAUDE.md's "Built-in log viewer" section: the system source's
    # rules aren't user-configurable.
    if source.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="System source rules are not editable"
        )


def _ordered_rules(session: Session, source_id: int) -> list[Rule]:
    return list(
        session.exec(select(Rule).where(Rule.source_id == source_id).order_by(Rule.order)).all()
    )


@router.get("", response_model=list[RulePublic])
def list_rules(source: Source = Depends(require_view), session: Session = Depends(get_session)):
    return _ordered_rules(session, source.id)


@router.post("", response_model=RulePublic, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: RuleCreate,
    source: Source = Depends(require_manage_rules),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    _require_editable_rules(source)
    pattern, pattern_kind = parse_pattern(payload.pattern)

    existing = _ordered_rules(session, source.id)
    next_order = (existing[-1].order + 1) if existing else 0

    rule = Rule(
        source_id=source.id,
        order=next_order,
        type=payload.type,
        pattern=pattern,
        pattern_kind=pattern_kind,
        notes=payload.notes,
    )
    session.add(rule)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="rule.create",
        target_type="rule",
        target_id=rule.id,
        metadata={"source_id": source.id, "pattern": pattern},
    )
    session.commit()
    session.refresh(rule)
    return rule


@router.patch("/{rule_id}", response_model=RulePublic)
def update_rule(
    rule_id: int,
    payload: RuleUpdate,
    source: Source = Depends(require_manage_rules),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    _require_editable_rules(source)
    rule = session.get(Rule, rule_id)
    if rule is None or rule.source_id != source.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")

    if payload.type is not None:
        rule.type = payload.type
    if payload.pattern is not None:
        rule.pattern, rule.pattern_kind = parse_pattern(payload.pattern)
    if "notes" in payload.model_fields_set:
        rule.notes = payload.notes

    session.add(rule)
    record_audit_event(
        session,
        user_id=user.id,
        action="rule.update",
        target_type="rule",
        target_id=rule.id,
        metadata={"source_id": source.id},
    )
    session.commit()
    session.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: int,
    source: Source = Depends(require_manage_rules),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    _require_editable_rules(source)
    rule = session.get(Rule, rule_id)
    if rule is None or rule.source_id != source.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")

    session.delete(rule)
    record_audit_event(
        session,
        user_id=user.id,
        action="rule.delete",
        target_type="rule",
        target_id=rule_id,
        metadata={"source_id": source.id},
    )
    session.commit()


@router.post("/reorder", response_model=list[RulePublic])
def reorder_rules(
    payload: ReorderRequest,
    source: Source = Depends(require_manage_rules),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    _require_editable_rules(source)
    existing = _ordered_rules(session, source.id)
    existing_ids = {rule.id for rule in existing}

    if set(payload.rule_ids) != existing_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rule_ids must be exactly the source's current rule ids",
        )

    by_id = {rule.id: rule for rule in existing}
    for index, rule_id in enumerate(payload.rule_ids):
        by_id[rule_id].order = index
        session.add(by_id[rule_id])

    record_audit_event(
        session,
        user_id=user.id,
        action="rule.reorder",
        target_type="source",
        target_id=source.id,
    )
    session.commit()
    return _ordered_rules(session, source.id)


def _parse_raw_line(line: str) -> tuple[RuleType, str] | None:
    """One line per rule, gitignore-style (CLAUDE.md's Web UI section: "a
    raw-text/gitignore-style paste mode for power users"): blank lines and
    `#`-comments are skipped, a leading `!` negates (marks the line as an
    exclude rule instead of include), and the existing admin-facing `re:`
    prefix convention still switches a line to regex. Order in the pasted
    text becomes rule order top-to-bottom, matching last-match-wins."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("!"):
        return RuleType.exclude, stripped[1:].strip()
    return RuleType.include, stripped


@router.put("/raw", response_model=list[RulePublic])
def replace_rules_raw(
    payload: RawRulesRequest,
    source: Source = Depends(require_manage_rules),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    """Bulk-replaces the source's entire rule chain from pasted text — the
    power-user alternative to the row-based editor, not a merge."""
    _require_editable_rules(source)
    parsed = [
        parsed_line
        for line in payload.text.splitlines()
        if (parsed_line := _parse_raw_line(line)) is not None
    ]

    for rule in _ordered_rules(session, source.id):
        session.delete(rule)
    session.flush()

    created: list[Rule] = []
    for order, (rule_type, raw_pattern) in enumerate(parsed):
        pattern, pattern_kind = parse_pattern(raw_pattern)
        rule = Rule(
            source_id=source.id,
            order=order,
            type=rule_type,
            pattern=pattern,
            pattern_kind=pattern_kind,
        )
        session.add(rule)
        created.append(rule)

    record_audit_event(
        session,
        user_id=user.id,
        action="rule.replace_raw",
        target_type="source",
        target_id=source.id,
        metadata={"rule_count": len(created)},
    )
    session.commit()
    for rule in created:
        session.refresh(rule)
    return created
