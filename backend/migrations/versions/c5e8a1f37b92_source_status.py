"""source status

Revision ID: c5e8a1f37b92
Revises: b3d7e9f21c56
Create Date: 2026-11-06 22:00:00.000000

Each source's last known reachability (#276), kept on the server and
shared by every user. Null until a check or an operation records it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "c5e8a1f37b92"
down_revision: str | Sequence[str] | None = "b3d7e9f21c56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.add_column(sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column("status_checked_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("status_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.drop_column("status_message")
        batch_op.drop_column("status_checked_at")
        batch_op.drop_column("status")
