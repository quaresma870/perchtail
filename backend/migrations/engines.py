"""Which database engine a migration runs on (#306).

The revisions up to a7c3e9d2f514 were written for SQLite (FTS5, sqlite_master,
sqlite_sequence, tables rebuilt in batch mode). A PostgreSQL database never
had any of them applied: it starts empty and gets the whole schema from the
pg_baseline revision that follows them. Each of those revisions starts with

    if not sqlite_only():
        return

so on PostgreSQL it does nothing and only moves the version on. Revisions
after pg_baseline run on both engines and don't use this.

op.get_context() works offline (`alembic upgrade --sql`) too, unlike
op.get_bind()."""

from alembic import op


def sqlite_only() -> bool:
    """True when this SQLite-only revision should run: the database is
    SQLite. False on any other engine, where it is skipped."""
    return op.get_context().dialect.name == "sqlite"


def postgresql_only() -> bool:
    """True when the database is PostgreSQL (the pg_baseline revision)."""
    return op.get_context().dialect.name == "postgresql"
