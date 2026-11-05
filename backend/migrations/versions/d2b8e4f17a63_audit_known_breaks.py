"""audit known breaks

Revision ID: d2b8e4f17a63
Revises: c3f9a2d4e815
Create Date: 2026-11-05 18:00:00.000000

Known breaks in the audit hash chain from the #213 race (#217). The cutoff
is the newest audit row at the time of this upgrade: only rows up to it can
be marked as known breaks. On a fresh install the table is empty, so the
cutoff stays null and nothing can be marked.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2b8e4f17a63"
down_revision: str | Sequence[str] | None = "c3f9a2d4e815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audit_chain_state") as batch_op:
        batch_op.add_column(sa.Column("known_break_cutoff_row_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("race_break_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("known_break_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("known_breaks_recorded_at", sa.DateTime(), nullable=True))

    bind = op.get_bind()
    cutoff = bind.execute(sa.text("SELECT MAX(id) FROM auditlog")).scalar()
    if cutoff is None:
        return
    has_state = bind.execute(sa.text("SELECT COUNT(*) FROM audit_chain_state")).scalar()
    if has_state:
        bind.execute(
            sa.text("UPDATE audit_chain_state SET known_break_cutoff_row_id = :cutoff"),
            {"cutoff": cutoff},
        )
    else:
        bind.execute(
            sa.text(
                "INSERT INTO audit_chain_state (status, known_break_cutoff_row_id) "
                "VALUES ('unknown', :cutoff)"
            ),
            {"cutoff": cutoff},
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_chain_state") as batch_op:
        batch_op.drop_column("known_breaks_recorded_at")
        batch_op.drop_column("known_break_count")
        batch_op.drop_column("race_break_count")
        batch_op.drop_column("known_break_cutoff_row_id")
