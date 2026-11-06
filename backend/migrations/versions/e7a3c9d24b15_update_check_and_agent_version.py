"""update check and agent version

Revision ID: e7a3c9d24b15
Revises: d2b8e4f17a63
Create Date: 2026-11-06 12:00:00.000000

The release check's state (#215) and the version an agent reports on
connect. Null for existing sources until their agent reconnects with 0.6 or
later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "e7a3c9d24b15"
down_revision: str | Sequence[str] | None = "d2b8e4f17a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "update_check_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("etag", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("latest_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("latest_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("latest_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("latest_published_at", sa.DateTime(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("notified_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # The singleton row (app/update_check.py's STATE_ID), created here so
    # the daily job and a "Check now" never race to insert it.
    op.execute("INSERT INTO update_check_state (id) VALUES (1)")
    with op.batch_alter_table("source") as batch_op:
        batch_op.add_column(
            sa.Column("agent_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.drop_column("agent_version")
    op.drop_table("update_check_state")
