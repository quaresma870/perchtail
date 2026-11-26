"""drop source.schedule_cron

Revision ID: e5c1b7a93d24
Revises: d4a8f2c61e35
Create Date: 2026-11-15 10:00:00.000000

source.schedule_cron came from the first schema, when sources were synced on
a schedule. Nothing has read or written it since browsing became live, so
the column goes. The table is rebuilt with AUTOINCREMENT (app.models.
NO_ID_REUSE). Going down adds it back, empty.

Can be run again after an interruption (see _recover_interrupted_rebuild).
"""

import logging
import sys
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from migrations.engines import sqlite_only

revision: str = "e5c1b7a93d24"
down_revision: str | Sequence[str] | None = "d4a8f2c61e35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NO_ID_REUSE = {"sqlite_autoincrement": True}

logger = logging.getLogger("alembic.runtime.migration")


def _recover_interrupted_rebuild(table: str) -> None:
    """SQLite DDL isn't transactional: a batch rebuild runs CREATE TABLE
    _alembic_tmp_<table>, copies the rows into it, drops <table> and renames
    the copy, each step on its own, and an interruption can leave the
    temporary table behind. What it holds depends on where it stopped:

    - <table> still exists: the copy was never finished or never replaced
      it, so it is a leftover and is dropped;
    - only the copy exists: it stopped after the DROP, so the copy holds
      every row; the rebuild is finished by renaming it, and logged;
    - neither exists: nothing can be recovered here, so the migration stops
      and points at the pre-upgrade backup, without touching anything.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    temporary = f"_alembic_tmp_{table}"
    if inspector.has_table(table):
        if inspector.has_table(temporary):
            op.execute(sa.text(f'DROP TABLE "{temporary}"'))
        return
    if inspector.has_table(temporary):
        op.execute(sa.text(f'ALTER TABLE "{temporary}" RENAME TO "{table}"'))
        logger.warning(
            "An interrupted rebuild of table %s was finished: %s renamed back to %s.",
            table,
            temporary,
            table,
        )
        return
    logger.error(
        "Table %s is missing and there is no %s to recover it from. Restore the "
        "pre-upgrade backup (python -m app.backup restore) and run the upgrade again.",
        table,
        temporary,
    )
    sys.exit(1)


def _source_columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("source")}


def _source_sequence() -> int | None:
    if op.get_bind().dialect.name != "sqlite":
        return None
    return op.get_bind().execute(
        sa.text("SELECT seq FROM sqlite_sequence WHERE name = 'source'")
    ).scalar()


def _restore_source_sequence(seq: int | None) -> None:
    """The rebuild drops the old table and with it its sqlite_sequence row,
    so ids of sources deleted before it would be handed out again."""
    if seq is None:
        return
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'source'"))
    bind.execute(
        sa.text(
            "INSERT INTO sqlite_sequence (name, seq) "
            "VALUES ('source', MAX(:seq, (SELECT COALESCE(MAX(id), 0) FROM source)))"
        ),
        {"seq": seq},
    )


def upgrade() -> None:
    if not sqlite_only():
        return
    _recover_interrupted_rebuild("source")
    if "schedule_cron" not in _source_columns():
        return
    seq = _source_sequence()
    with op.batch_alter_table("source", recreate="always", table_kwargs=NO_ID_REUSE) as batch:
        batch.drop_column("schedule_cron")
    _restore_source_sequence(seq)


def downgrade() -> None:
    if not sqlite_only():
        return
    _recover_interrupted_rebuild("source")
    if "schedule_cron" in _source_columns():
        return
    seq = _source_sequence()
    with op.batch_alter_table("source", recreate="always", table_kwargs=NO_ID_REUSE) as batch:
        batch.add_column(
            sa.Column("schedule_cron", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
    _restore_source_sequence(seq)
