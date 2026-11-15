"""audit log: origin, hash format per row, no id reuse, minimum retention

Revision ID: c7f2a9e4d136
Revises: b9e4d7a2c513
Create Date: 2026-11-14 12:00:00.000000

Adds client_ip, user_agent and session_id to auditlog, and hash_format,
which says which fields a row's hash covers (app/audit_hash_chain.py).
Every existing row keeps a null hash_format, so it is still verified by the
format it was written with.

Rebuilds auditlog with AUTOINCREMENT (see app.models.NO_ID_REUSE), so an id
the purge deleted is never handed out again, and starts its sequence past
every id it has used: the newest row, or the purge anchor when that is
higher. The rebuild copies every row as it is, so the hash chain is
unchanged.

Adds the audit_outbox table (app/audit_outbox.py): events a migration
records without the key the chain is signed with, which the next start
writes to the audit log as ordinary signed entries at the end of the chain.

Raises an audit retention stored between 1 and 89 days to 90, the new
minimum (0, keep forever, stays), and records it in the audit outbox as a
system_settings.update with the old and new values; the next start records
it in the audit log with no user. Nothing is written to auditlog here: a row
without a hash is never signed later, and is reported as tampering.

Can be run again after an interruption: a leftover _alembic_tmp_auditlog is
dropped while auditlog exists, or renamed back to auditlog when the rebuild
stopped after dropping it (see _recover_interrupted_rebuild), and columns and
tables that already exist aren't added again.

Going down drops the columns, the outbox and AUTOINCREMENT; rows written in
format 2 then no longer verify under the older version, since part of what
their hash covers is gone. A raised retention is not lowered again.
"""

import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "c7f2a9e4d136"
down_revision: str | Sequence[str] | None = "b9e4d7a2c513"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NO_ID_REUSE = {"sqlite_autoincrement": True}
AUDIT_RETENTION_MIN_DAYS = 90

logger = logging.getLogger("alembic.runtime.migration")

_audit_outbox = sa.table(
    "audit_outbox",
    sa.column("action", sa.String),
    sa.column("metadata", sa.JSON),
    sa.column("created_at", sa.DateTime),
)


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


def upgrade() -> None:
    bind = op.get_bind()
    _recover_interrupted_rebuild("auditlog")
    existing = {column["name"] for column in sa.inspect(bind).get_columns("auditlog")}
    new_columns = [
        sa.Column("client_ip", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("user_agent", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("hash_format", sa.Integer(), nullable=True),
    ]
    with op.batch_alter_table("auditlog", recreate="always", table_kwargs=NO_ID_REUSE) as batch:
        for column in new_columns:
            if column.name not in existing:
                batch.add_column(column)
    if not sa.inspect(bind).has_table("audit_outbox"):
        op.create_table(
            "audit_outbox",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("action", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    high_water = bind.execute(
        sa.text(
            "SELECT MAX(m) FROM (SELECT MAX(id) AS m FROM auditlog "
            "UNION ALL SELECT MAX(anchor_row_id) FROM audit_chain_state)"
        )
    ).scalar()
    if high_water is not None:
        bind.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'auditlog'"))
        bind.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('auditlog', :seq)"),
            {"seq": high_water},
        )

    _raise_short_retention(bind)


def _raise_short_retention(bind) -> None:
    stored = bind.execute(
        sa.text("SELECT value FROM system_setting WHERE key = 'audit_retention_days'")
    ).scalar()
    if stored is None:
        return
    try:
        days = int(stored)
    except ValueError:
        return
    if not 0 < days < AUDIT_RETENTION_MIN_DAYS:
        return
    bind.execute(
        sa.text("UPDATE system_setting SET value = :value WHERE key = 'audit_retention_days'"),
        {"value": str(AUDIT_RETENTION_MIN_DAYS)},
    )
    bind.execute(
        _audit_outbox.insert().values(
            action="system_settings.update",
            metadata={"audit_retention_days": {"old": days, "new": AUDIT_RETENTION_MIN_DAYS}},
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )


def downgrade() -> None:
    op.drop_table("audit_outbox")
    _recover_interrupted_rebuild("auditlog")
    with op.batch_alter_table(
        "auditlog", recreate="always", table_kwargs={"sqlite_autoincrement": False}
    ) as batch:
        batch.drop_column("hash_format")
        batch.drop_column("session_id")
        batch.drop_column("user_agent")
        batch.drop_column("client_ip")
