"""source customer from folder

Revision ID: a7c3e9d2f514
Revises: f6a2d8c41b73
Create Date: 2026-11-16 12:00:00.000000

A source in a folder belongs to the folder's customer: the API now refuses
any other customer_id, or none, with a folder. Sources saved before with a
customer_id that isn't their folder's (or none) are given the folder's, so
the customer's grants govern them like every other source there. Only rows,
no table is rebuilt.

That changes who reaches those sources, so each source corrected is recorded
in the audit outbox (app/audit_outbox.py) as a source.update with reason
folder_customer and its customer_id before and after; the next start
records it in the audit log with no user. Nothing is written to auditlog
here (see c7f2a9e4d136).

It runs in the migration's transaction, so it is all or nothing, and it can
be run again after an interruption: rows already corrected aren't matched,
and their outbox entries aren't written again. Going down changes nothing,
since the previous customer_id was wrong and isn't kept.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from migrations.engines import sqlite_only

revision: str = "a7c3e9d2f514"
down_revision: str | Sequence[str] | None = "f6a2d8c41b73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_audit_outbox = sa.table(
    "audit_outbox",
    sa.column("action", sa.String),
    sa.column("metadata", sa.JSON),
    sa.column("created_at", sa.DateTime),
)

_MISMATCHED = (
    "SELECT source.id, source.folder_id, source.customer_id, folder.customer_id "
    "FROM source JOIN folder ON folder.id = source.folder_id "
    "WHERE folder.customer_id IS NOT NULL "
    "AND (source.customer_id IS NULL OR source.customer_id != folder.customer_id) "
    "ORDER BY source.id"
)


def upgrade() -> None:
    if not sqlite_only():
        return
    bind = op.get_bind()
    rows = bind.execute(sa.text(_MISMATCHED)).fetchall()
    for source_id, folder_id, old_customer_id, new_customer_id in rows:
        bind.execute(
            sa.text("UPDATE source SET customer_id = :customer_id WHERE id = :id"),
            {"customer_id": new_customer_id, "id": source_id},
        )
        bind.execute(
            _audit_outbox.insert().values(
                action="source.update",
                metadata={
                    "reason": "folder_customer",
                    "source_id": source_id,
                    "folder_id": folder_id,
                    "changes": {"customer_id": {"from": old_customer_id, "to": new_customer_id}},
                },
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    if rows:
        logger.warning(
            "Set the customer of %d source(s) to the customer of their folder: %s.",
            len(rows),
            ", ".join(str(row[0]) for row in rows),
        )


def downgrade() -> None:
    if not sqlite_only():
        return
    pass
