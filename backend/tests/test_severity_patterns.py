from app.models import Customer, PatternKind, Protocol, SeverityLevel, SeverityPattern, Source
from app.severity_patterns import (
    DEFAULT_GLOBAL_PATTERNS,
    effective_patterns,
    seed_default_global_patterns,
)


def _make_source(session) -> Source:
    customer = Customer(name="Vodacom Tanzania")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = Source(
        name="app01",
        customer_id=customer.id,
        protocol=Protocol.ssh,
        host="app01",
        base_path="/var/log",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def test_effective_falls_back_to_global_when_source_has_no_overrides(session):
    source = _make_source(session)
    global_pattern = SeverityPattern(source_id=None, level=SeverityLevel.error, pattern="error")
    session.add(global_pattern)
    session.commit()

    result = effective_patterns(session, source.id)
    assert [p.id for p in result] == [global_pattern.id]


def test_effective_prefers_source_overrides_and_does_not_merge(session):
    source = _make_source(session)
    session.add(SeverityPattern(source_id=None, level=SeverityLevel.error, pattern="error"))
    override = SeverityPattern(source_id=source.id, level=SeverityLevel.warning, pattern="retry")
    session.add(override)
    session.commit()

    result = effective_patterns(session, source.id)
    assert [p.id for p in result] == [override.id]


def test_effective_returns_empty_when_neither_scope_has_patterns(session):
    source = _make_source(session)
    assert effective_patterns(session, source.id) == []


def test_seed_default_global_patterns_is_idempotent(session):
    from sqlmodel import select

    seed_default_global_patterns(session)
    seeded = session.exec(select(SeverityPattern)).all()
    assert len(seeded) == len(DEFAULT_GLOBAL_PATTERNS)
    assert all(p.source_id is None for p in seeded)

    seed_default_global_patterns(session)
    seeded_again = session.exec(select(SeverityPattern)).all()
    assert len(seeded_again) == len(DEFAULT_GLOBAL_PATTERNS)


def test_seed_default_global_patterns_parses_re_prefix(session):
    seed_default_global_patterns(session)

    from sqlmodel import select

    error_patterns = session.exec(
        select(SeverityPattern).where(SeverityPattern.level == SeverityLevel.error)
    ).all()
    assert error_patterns
    assert all(p.pattern_kind == PatternKind.regex for p in error_patterns)
    assert all(not p.pattern.startswith("re:") for p in error_patterns)
