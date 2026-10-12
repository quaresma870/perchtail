"""no id reuse after delete

Revision ID: 47c9d392f28f
Revises: 6ba4603526f4
Create Date: 2026-10-09 21:30:00.000000

Plain SQLite INTEGER PRIMARY KEY hands out max(id)+1, so deleting the
newest customer/folder/source/role and creating another reused its id --
and anything left pointing at the old row by id (a RoleGrant, search index
rows, an alert, an SSO group mapping) silently attached to the new one.

This migration (1) deletes rows already orphaned that way, (2) rebuilds the
four tables with AUTOINCREMENT, and (3) starts each table's id sequence past
every id the audit log has ever recorded for it, so an id deleted before
this migration ran can't be handed out again either.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "47c9d392f28f"
down_revision: str | Sequence[str] | None = "6ba4603526f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("customer", "folder", "source", "role")

_ORPHAN_CLEANUP = (
    "DELETE FROM rolegrant WHERE "
    "(scope_type = 'customer' AND scope_id NOT IN (SELECT id FROM customer)) OR "
    "(scope_type = 'folder' AND scope_id NOT IN (SELECT id FROM folder)) OR "
    "(scope_type = 'source' AND scope_id NOT IN (SELECT id FROM source))",
    "DELETE FROM rule WHERE source_id NOT IN (SELECT id FROM source)",
    "DELETE FROM severity_pattern "
    "WHERE source_id IS NOT NULL AND source_id NOT IN (SELECT id FROM source)",
    "DELETE FROM search_index_state WHERE source_id NOT IN (SELECT id FROM source)",
    # An alert scoped to a deleted source is deleted, not unscoped: a NULL
    # source_id means "every source the owner can view", a broader scope.
    "DELETE FROM alert WHERE source_id IS NOT NULL AND source_id NOT IN (SELECT id FROM source)",
    "DELETE FROM sso_group_role_mapping WHERE role_id NOT IN (SELECT id FROM role)",
)


def _rebuild(autoincrement: bool) -> None:
    for table in _TABLES:
        with op.batch_alter_table(
            table, recreate="always", table_kwargs={"sqlite_autoincrement": autoincrement}
        ):
            pass


def upgrade() -> None:
    bind = op.get_bind()
    for statement in _ORPHAN_CLEANUP:
        bind.execute(sa.text(statement))
    has_fts = bind.execute(
        sa.text("SELECT 1 FROM sqlite_master WHERE name = 'search_index_fts'")
    ).first()
    if has_fts:
        bind.execute(
            sa.text("DELETE FROM search_index_fts WHERE source_id NOT IN (SELECT id FROM source)")
        )

    _rebuild(autoincrement=True)

    for table in _TABLES:
        high_water = bind.execute(
            sa.text(
                f"SELECT MAX(m) FROM ("  # noqa: S608 - table names are the fixed constants above
                f"SELECT MAX(id) AS m FROM {table} "
                "UNION ALL SELECT MAX(target_id) FROM auditlog WHERE target_type = :t)"
            ),
            {"t": table},
        ).scalar()
        if high_water is None:
            continue
        bind.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = :t"), {"t": table})
        bind.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES (:t, :seq)"),
            {"t": table, "seq": high_water},
        )


def downgrade() -> None:
    # The orphan cleanup isn't reversible (and nothing would want it back);
    # only the table definitions revert.
    _rebuild(autoincrement=False)
