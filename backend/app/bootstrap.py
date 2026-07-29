import secrets

from sqlmodel import Session, select

from app.auth.models import User
from app.auth.providers.local import create_local_user
from app.auth.rbac import create_role
from app.config import get_settings
from app.logging_config import get_logger
from app.models import PatternKind, Protocol, Rule, RuleType, Source

logger = get_logger(__name__)

SYSTEM_SOURCE_NAME = "PerchTail application logs"
SUPER_ADMIN_ROLE_NAME = "Super Admin"


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


def seed_initial_super_admin(session: Session) -> User | None:
    """A fresh deployment has zero users and no way to log in — CLAUDE.md's
    LocalPasswordProvider is "the break-glass super-admin account," but
    something has to create the first one. No-op once any user exists (this
    is strictly a first-run bootstrap, not something that should ever touch
    an existing deployment). Generates a random password rather than
    reading one from config — same reasoning as the admin-driven
    reset-password endpoint (api/users.py): a temporary credential logged
    once and forced to change beats a long-lived secret sitting in a .env
    file. Logged at WARNING so it isn't lost in routine INFO noise."""
    if session.exec(select(User)).first() is not None:
        return None

    settings = get_settings()
    role = create_role(
        session,
        actor_user_id=None,
        name=SUPER_ADMIN_ROLE_NAME,
        is_builtin=True,
        is_super_admin=True,
    )
    temporary_password = secrets.token_urlsafe(16)
    user = create_local_user(
        session,
        actor_user_id=None,
        username=settings.initial_admin_username,
        password=temporary_password,
        role_id=role.id,
    )

    logger.warning(
        "initial_super_admin.seeded",
        username=user.username,
        temporary_password=temporary_password,
        note="Log in and change this password immediately — it will not be shown again.",
    )
    return user
