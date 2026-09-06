from sqlmodel import Session, select

from app.models import PatternKind, SeverityLevel, SeverityPattern

# Seeded once on first startup (see app.bootstrap.seed_default_severity_patterns)
# so the global set isn't empty out of the box -- covers the common markers
# ROADMAP.md calls out beyond the old hardcoded [error]/[warn] tokens:
# critical/severe/panic/exception/traceback-style words other ecosystems use.
DEFAULT_GLOBAL_PATTERNS: list[dict] = [
    {
        "level": SeverityLevel.error,
        "pattern": r"re:\b(error|fatal|critical|panic)\b",
        "highlight_line": True,
        "include_in_navigation": True,
    },
    {
        "level": SeverityLevel.error,
        "pattern": r"re:\b(exception|traceback)\b",
        "highlight_line": True,
        "include_in_navigation": True,
    },
    {
        "level": SeverityLevel.warning,
        "pattern": r"re:\b(warn|warning)\b",
        "highlight_line": False,
        "include_in_navigation": True,
    },
    # info/debug are routine, not "problems" -- highlighted for readability
    # but left out of next/previous-problem navigation by default (an admin
    # can still flip this per pattern; see SeverityPattern.include_in_navigation).
    {
        "level": SeverityLevel.info,
        "pattern": r"re:\binfo\b",
        "highlight_line": False,
        "include_in_navigation": False,
    },
    {
        "level": SeverityLevel.debug,
        "pattern": r"re:\b(debug|trace)\b",
        "highlight_line": False,
        "include_in_navigation": False,
    },
]


def effective_patterns(session: Session, source_id: int) -> list[SeverityPattern]:
    """A source's own patterns win outright when it has any at all, else the
    global default set -- same "most specific wins" shape as grant
    resolution (auth/rbac.py), just for pattern configuration instead of
    permissions. Not merged: an admin who overrides a source starts from a
    clean slate for that source, not the global set plus additions."""
    own = list(
        session.exec(select(SeverityPattern).where(SeverityPattern.source_id == source_id)).all()
    )
    if own:
        return own
    return list(
        session.exec(select(SeverityPattern).where(SeverityPattern.source_id.is_(None))).all()
    )


def seed_default_global_patterns(session: Session) -> None:
    """Idempotent first-run seed -- a no-op once any global pattern exists,
    same pattern as the other bootstrap seeds (app/bootstrap.py)."""
    existing = session.exec(
        select(SeverityPattern).where(SeverityPattern.source_id.is_(None))
    ).first()
    if existing is not None:
        return

    for spec in DEFAULT_GLOBAL_PATTERNS:
        raw = spec["pattern"]
        pattern = raw[len("re:") :] if raw.startswith("re:") else raw
        pattern_kind = PatternKind.regex if raw.startswith("re:") else PatternKind.glob
        session.add(
            SeverityPattern(
                source_id=None,
                level=spec["level"],
                pattern=pattern,
                pattern_kind=pattern_kind,
                highlight_line=spec["highlight_line"],
                include_in_navigation=spec["include_in_navigation"],
            )
        )
    session.commit()
