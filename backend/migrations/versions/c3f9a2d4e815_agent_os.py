"""agent os

Revision ID: c3f9a2d4e815
Revises: b7d4f2a9c610
Create Date: 2026-11-04 14:00:00.000000

The OS an agent reports on connect (#189), so rules for a Windows agent are
matched case-insensitively. Null for existing sources until their agent
reconnects with 0.5.1 or later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "c3f9a2d4e815"
down_revision: str | Sequence[str] | None = "b7d4f2a9c610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.add_column(
            sa.Column("agent_os", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.drop_column("agent_os")
