from sqlmodel import Session, select

from app.config import get_settings
from app.logging_config import get_logger
from app.models import PatternKind, Protocol, Rule, RuleType, Source

logger = get_logger(__name__)

SYSTEM_SOURCE_NAME = "PerchTail application logs"


def seed_system_log_source(session: Session) -> Source:
    """Seeds the built-in, super-admin-only log viewer on first startup —
    dogfooding the same live-browsing/viewer path as customer sources, via
    the `local` protocol pointed at LOG_DIR (CLAUDE.md's "Built-in log
    viewer" section). Idempotent: a no-op on every startup after the
    first. Access is gated purely by is_system + is_super_admin in
    auth/rbac.py's resolve_capability — no grant, however permissive, can
    reach it.

    Seeds one broad include rule (LOG_DIR is a dedicated directory that
    should only ever contain this app's own rotated logs, and the source
    is already super-admin-only) rather than leaving it with zero rules,
    which would make the viewer show nothing at all."""
    existing = session.exec(select(Source).where(Source.is_system.is_(True))).first()
    if existing is not None:
        return existing

    settings = get_settings()
    source = Source(
        name=SYSTEM_SOURCE_NAME,
        customer_id=None,
        folder_id=None,
        protocol=Protocol.local,
        host="localhost",
        base_path=settings.log_dir,
        is_system=True,
    )
    session.add(source)
    session.flush()

    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**/*",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    session.refresh(source)

    logger.info("system_source.seeded", source_id=source.id, log_dir=settings.log_dir)
    return source
