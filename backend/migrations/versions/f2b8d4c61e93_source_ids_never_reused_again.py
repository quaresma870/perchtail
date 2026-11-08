"""source ids never reused, again

Revision ID: f2b8d4c61e93
Revises: c8e3f1a62d07
Create Date: 2026-11-08 20:00:00.000000

The file-name index migration (e4b7c2d91a05) rebuilt the source table
without AUTOINCREMENT, which 47c9d392f28f had added so a deleted source's id
is never handed out again (anything still pointing at it by id -- a grant,
an alert -- would otherwise attach to the new source). Those migrations now
keep it, but a database that already ran them lost it: this rebuilds the
table with AUTOINCREMENT when it's missing and starts the id sequence past
every source id the table or the audit log has seen. Nothing to do on a
database that never lost it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b8d4c61e93"
down_revision: str | Sequence[str] | None = "c8e3f1a62d07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    table_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'source'")
    ).scalar()
    if table_sql and "AUTOINCREMENT" not in table_sql.upper():
        with op.batch_alter_table(
            "source", recreate="always", table_kwargs={"sqlite_autoincrement": True}
        ):
            pass
    high_water = bind.execute(
        sa.text(
            "SELECT MAX(m) FROM (SELECT MAX(id) AS m FROM source "
            "UNION ALL SELECT MAX(target_id) FROM auditlog WHERE target_type = 'source')"
        )
    ).scalar()
    current = bind.execute(
        sa.text("SELECT seq FROM sqlite_sequence WHERE name = 'source'")
    ).scalar()
    if high_water is not None and (current is None or current < high_water):
        bind.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'source'"))
        bind.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('source', :seq)"),
            {"seq": high_water},
        )


def downgrade() -> None:
    # Keeping AUTOINCREMENT is what every earlier revision wants too.
    pass
