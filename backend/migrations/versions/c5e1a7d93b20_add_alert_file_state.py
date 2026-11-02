"""add alert_file_state

Revision ID: c5e1a7d93b20
Revises: 47c9d392f28f
Create Date: 2026-11-02 23:30:00.000000

Per-alert, per-file "reported through line N" bookkeeping, so a re-indexed
file only fires an alert for its new lines rather than every old match.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "c5e1a7d93b20"
down_revision: str | Sequence[str] | None = "47c9d392f28f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_file_state",
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("reported_through_line", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["alert.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("alert_id", "source_id", "file_path"),
    )


def downgrade() -> None:
    op.drop_table("alert_file_state")
