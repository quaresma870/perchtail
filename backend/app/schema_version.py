"""Which Alembic revision the database is at, and whether this app knows it
(#334).

A database migrated by a newer PerchTail is at a revision this image's
migrations don't have. Going on would run this version's code against a
schema it doesn't know, and Alembic itself would fail with "Can't locate
revision". Startup (app/main.py) and `alembic upgrade` (migrations/env.py)
refuse first, with `startup.database_newer_than_app`."""

from functools import lru_cache
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from app.version import APP_VERSION

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class DatabaseNewerThanAppError(RuntimeError):
    def __init__(self, revision: str):
        self.revision = revision
        super().__init__(
            f"the database is at revision {revision}, which PerchTail {APP_VERSION} doesn't "
            "know: it was migrated by a newer version. Run that version, or restore a "
            "backup taken before the upgrade."
        )


@lru_cache(maxsize=1)
def _known_revisions() -> frozenset[str]:
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    return frozenset(rev.revision for rev in script.walk_revisions())


def database_revision(engine: Engine) -> str | None:
    """The revision in the database's alembic_version table, or None for a
    database Alembic never stamped (a new one)."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_known_revision(rev: str | None) -> bool:
    """None (not stamped yet) counts as known: there's nothing newer there."""
    return rev is None or rev in _known_revisions()


def check_database_revision(engine: Engine, logger) -> None:
    """Logs `startup.database_newer_than_app` and raises
    DatabaseNewerThanAppError when the database is at an unknown revision."""
    revision = database_revision(engine)
    if is_known_revision(revision):
        return
    log_database_newer_than_app(logger, revision)
    raise DatabaseNewerThanAppError(revision)


def log_database_newer_than_app(logger, revision: str | None) -> None:
    """The CRITICAL event every refusal logs (docs/monitoring.md)."""
    logger.critical(
        "startup.database_newer_than_app",
        database_revision=revision,
        app_version=APP_VERSION,
        hint="the database was migrated by a newer PerchTail; run that version "
        "or restore a backup taken before the upgrade",
    )
