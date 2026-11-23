"""Redaction rules (#382, #249, app/redaction_rules.py). Every endpoint is
for super admins only: redaction decides what everyone, super admins
included, sees of file content, so no delegated capability (not even
manage_rules or manage_system_settings) reaches it. Every change is
audited."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.api.auth import require_super_admin
from app.audit import record_audit_event
from app.auth.models import User
from app.db import get_session
from app.models import RedactionRule, Source, SourceRedactionOverride
from app.redaction import redact_bytes
from app.redaction_rules import (
    MAX_NAME_CHARS,
    MAX_PATTERN_CHARS,
    MAX_RULE_LENGTH,
    MAX_RULES_PER_SCOPE,
    RedactionRuleInvalid,
    check_custom_rule,
    check_name,
    compile_checked,
    effective_redaction_rules,
    source_rules,
    system_rules,
)

# The most text the pattern test runs on.
MAX_SAMPLE_CHARS = 65536


class RedactionRulePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int | None
    name: str
    # A built-in rule (one of the engine's detectors) has a detector and no
    # pattern; a custom rule the other way round.
    detector: str | None
    pattern: str | None
    secret_group: int | None
    max_length: int | None
    enabled: bool
    order: int
    notes: str | None


class RedactionRuleCreate(BaseModel):
    """A custom rule; the built-in ones are seeded and can't be added."""

    name: str = Field(max_length=MAX_NAME_CHARS * 4)
    pattern: str = Field(max_length=MAX_PATTERN_CHARS)
    # The longest match the pattern can make, in bytes.
    max_length: int = Field(ge=1, le=MAX_RULE_LENGTH)
    # The capture group to replace; none replaces the whole match.
    secret_group: int | None = Field(default=None, ge=0)
    enabled: bool = True
    # None puts it after the scope's other rules.
    order: int | None = None
    notes: str | None = None


class RedactionRuleUpdate(BaseModel):
    """name, pattern, max_length and secret_group are for custom rules only."""

    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS * 4)
    pattern: str | None = Field(default=None, max_length=MAX_PATTERN_CHARS)
    max_length: int | None = Field(default=None, ge=1, le=MAX_RULE_LENGTH)
    secret_group: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    order: int | None = None
    notes: str | None = None


class RedactionOverrideSet(BaseModel):
    # True turns the system rule off for this source; false turns it back on.
    disabled: bool


class RedactionOverridePublic(BaseModel):
    source_id: int
    rule_id: int
    disabled: bool


class RedactionTestRequest(BaseModel):
    pattern: str = Field(max_length=MAX_PATTERN_CHARS)
    secret_group: int | None = Field(default=None, ge=0)
    # Text the admin types: never a file's content.
    sample: str = Field(max_length=MAX_SAMPLE_CHARS)


class RedactionTestMatch(BaseModel):
    # Character offsets in the sample of what the rule replaces.
    start: int
    end: int


class RedactionTestResult(BaseModel):
    redacted: str
    matches: list[RedactionTestMatch]


def _invalid(exc: RedactionRuleInvalid) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")


def _get_source(session: Session, source_id: int) -> Source:
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return source


def _scope_rules(session: Session, source_id: int | None) -> list[RedactionRule]:
    return system_rules(session) if source_id is None else source_rules(session, source_id)


def _scope_metadata(source_id: int | None) -> dict:
    return {"scope": "system"} if source_id is None else {"scope": "source", "source_id": source_id}


def _check_unique_name(
    session: Session, source_id: int | None, name: str, rule_id: int | None = None
) -> None:
    for rule in _scope_rules(session, source_id):
        if rule.id != rule_id and rule.name.casefold() == name.casefold():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A rule named {name!r} already exists here",
            )


def _create(
    session: Session, user: User, source_id: int | None, payload: RedactionRuleCreate
) -> RedactionRule:
    existing = _scope_rules(session, source_id)
    if len(existing) >= MAX_RULES_PER_SCOPE:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_RULES_PER_SCOPE} redaction rules here",
        )
    try:
        name = check_name(payload.name)
        check_custom_rule(payload.pattern, payload.secret_group, payload.max_length)
    except RedactionRuleInvalid as exc:
        raise _invalid(exc) from exc
    _check_unique_name(session, source_id, name)
    order = (
        payload.order
        if payload.order is not None
        else max((rule.order for rule in existing), default=-1) + 1
    )
    rule = RedactionRule(
        source_id=source_id,
        name=name,
        pattern=payload.pattern,
        secret_group=payload.secret_group,
        max_length=payload.max_length,
        enabled=payload.enabled,
        order=order,
        notes=payload.notes,
    )
    session.add(rule)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="redaction_rule.create",
        target_type="redaction_rule",
        target_id=rule.id,
        metadata={
            **_scope_metadata(source_id),
            "name": rule.name,
            "pattern": rule.pattern,
            "secret_group": rule.secret_group,
            "max_length": rule.max_length,
            "enabled": rule.enabled,
            "order": rule.order,
        },
    )
    session.commit()
    session.refresh(rule)
    return rule


_CUSTOM_ONLY = ("name", "pattern", "max_length", "secret_group")


def _update(
    session: Session, user: User, rule: RedactionRule, payload: RedactionRuleUpdate
) -> RedactionRule:
    sent = {key: value for key, value in payload.model_dump(exclude_unset=True).items()}
    # An explicit null means "no change", except for the two fields where
    # null is a value: the whole match (secret_group) and no notes.
    sent = {k: v for k, v in sent.items() if v is not None or k in ("secret_group", "notes")}
    if rule.detector is not None and set(sent) & set(_CUSTOM_ONLY):
        raise HTTPException(
            status_code=422,
            detail="A built-in rule can only be turned on or off, moved or annotated",
        )
    if "name" in sent:
        try:
            sent["name"] = check_name(sent["name"])
        except RedactionRuleInvalid as exc:
            raise _invalid(exc) from exc
        _check_unique_name(session, rule.source_id, sent["name"], rule.id)
    if rule.detector is None and set(sent) & {"pattern", "max_length", "secret_group"}:
        try:
            check_custom_rule(
                sent.get("pattern", rule.pattern),
                sent.get("secret_group", rule.secret_group),
                sent.get("max_length", rule.max_length),
            )
        except RedactionRuleInvalid as exc:
            raise _invalid(exc) from exc
    changes = {
        key: {"old": getattr(rule, key), "new": value}
        for key, value in sent.items()
        if getattr(rule, key) != value
    }
    for key, value in sent.items():
        setattr(rule, key, value)
    session.add(rule)
    record_audit_event(
        session,
        user_id=user.id,
        action="redaction_rule.update",
        target_type="redaction_rule",
        target_id=rule.id,
        metadata={**_scope_metadata(rule.source_id), "changes": changes},
    )
    session.commit()
    session.refresh(rule)
    return rule


def _delete(session: Session, user: User, rule: RedactionRule) -> None:
    if rule.detector is not None:
        raise HTTPException(
            status_code=422,
            detail="A built-in rule can't be deleted: turn it off instead",
        )
    rule_id = rule.id
    for override in session.exec(
        select(SourceRedactionOverride).where(SourceRedactionOverride.rule_id == rule_id)
    ).all():
        session.delete(override)
    session.delete(rule)
    record_audit_event(
        session,
        user_id=user.id,
        action="redaction_rule.delete",
        target_type="redaction_rule",
        target_id=rule_id,
        metadata={**_scope_metadata(rule.source_id), "name": rule.name},
    )
    session.commit()


# --- System rules -------------------------------------------------------------

global_router = APIRouter(prefix="/redaction-rules", tags=["redaction"])


@global_router.get("", response_model=list[RedactionRulePublic])
def list_system_rules(
    user: User = Depends(require_super_admin), session: Session = Depends(get_session)
):
    return system_rules(session)


@global_router.post("", response_model=RedactionRulePublic, status_code=status.HTTP_201_CREATED)
def create_system_rule(
    payload: RedactionRuleCreate,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    return _create(session, user, None, payload)


@global_router.post("/test", response_model=RedactionTestResult)
def test_rule(payload: RedactionTestRequest, user: User = Depends(require_super_admin)):
    """Runs a custom pattern on text the admin types, never on a file: what
    it would replace and the text redacted. Changes nothing."""
    try:
        check_custom_rule(payload.pattern, payload.secret_group, MAX_RULE_LENGTH)
    except RedactionRuleInvalid as exc:
        # Here max_length is the largest allowed, so only a pattern that can
        # match more than any rule may is refused for its length.
        raise _invalid(exc) from exc
    group = payload.secret_group or 0
    data = payload.sample.encode("utf-8")
    out, _result = redact_bytes(
        data,
        [
            {
                "name": "test",
                "pattern": payload.pattern,
                "secret_group": group,
                "max_length": MAX_RULE_LENGTH,
            }
        ],
    )
    regex, empty = compile_checked(payload.pattern)
    text = data if isinstance(empty, bytes) else payload.sample
    matches = []
    for match in regex.finditer(text):
        start, end = match.span(group)
        if start < 0 or start == end:
            continue
        if isinstance(text, bytes):
            start = len(text[:start].decode("utf-8", "ignore"))
            end = len(text[:end].decode("utf-8", "ignore"))
        matches.append(RedactionTestMatch(start=start, end=end))
    return RedactionTestResult(redacted=out.decode("utf-8", "replace"), matches=matches)


@global_router.patch("/{rule_id}", response_model=RedactionRulePublic)
def update_system_rule(
    rule_id: int,
    payload: RedactionRuleUpdate,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    rule = session.get(RedactionRule, rule_id)
    if rule is None or rule.source_id is not None:
        raise _not_found()
    return _update(session, user, rule, payload)


@global_router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_system_rule(
    rule_id: int,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    rule = session.get(RedactionRule, rule_id)
    if rule is None or rule.source_id is not None:
        raise _not_found()
    _delete(session, user, rule)


# --- A source's rules and overrides --------------------------------------------

source_router = APIRouter(prefix="/sources/{source_id}", tags=["redaction"])


@source_router.get("/redaction-rules", response_model=list[RedactionRulePublic])
def list_source_rules(
    source_id: int,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    source = _get_source(session, source_id)
    return source_rules(session, source.id)


@source_router.get("/redaction-rules/effective", response_model=list[RedactionRulePublic])
def get_effective_rules(
    source_id: int,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """The rules the engine runs on this source, in the order it runs them."""
    source = _get_source(session, source_id)
    return effective_redaction_rules(session, source.id)


@source_router.post(
    "/redaction-rules", response_model=RedactionRulePublic, status_code=status.HTTP_201_CREATED
)
def create_source_rule(
    source_id: int,
    payload: RedactionRuleCreate,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    source = _get_source(session, source_id)
    return _create(session, user, source.id, payload)


def _get_source_rule(session: Session, source_id: int, rule_id: int) -> RedactionRule:
    source = _get_source(session, source_id)
    rule = session.get(RedactionRule, rule_id)
    if rule is None or rule.source_id != source.id:
        raise _not_found()
    return rule


@source_router.patch("/redaction-rules/{rule_id}", response_model=RedactionRulePublic)
def update_source_rule(
    source_id: int,
    rule_id: int,
    payload: RedactionRuleUpdate,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    return _update(session, user, _get_source_rule(session, source_id, rule_id), payload)


@source_router.delete("/redaction-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source_rule(
    source_id: int,
    rule_id: int,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    _delete(session, user, _get_source_rule(session, source_id, rule_id))


@source_router.put("/redaction-overrides/{rule_id}", response_model=RedactionOverridePublic)
def set_override(
    source_id: int,
    rule_id: int,
    payload: RedactionOverrideSet,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Turns a system rule off (or back on) for this source."""
    source = _get_source(session, source_id)
    rule = session.get(RedactionRule, rule_id)
    if rule is None or rule.source_id is not None:
        raise _not_found()
    override = session.get(SourceRedactionOverride, (source.id, rule.id))
    if payload.disabled and override is None:
        session.add(SourceRedactionOverride(source_id=source.id, rule_id=rule.id, disabled=True))
    elif payload.disabled:
        override.disabled = True
        session.add(override)
    elif override is not None:
        session.delete(override)
    record_audit_event(
        session,
        user_id=user.id,
        action="redaction_override.set",
        target_type="redaction_rule",
        target_id=rule.id,
        metadata={"source_id": source.id, "disabled": payload.disabled},
    )
    session.commit()
    return RedactionOverridePublic(source_id=source.id, rule_id=rule.id, disabled=payload.disabled)
