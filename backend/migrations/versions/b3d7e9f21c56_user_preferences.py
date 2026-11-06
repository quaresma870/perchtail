"""user preferences

Revision ID: b3d7e9f21c56
Revises: a8e2f6c10d49
Create Date: 2026-11-06 20:00:00.000000

Per-user display preferences (#273): a JSON object on the user, null for
"the System defaults".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d7e9f21c56"
down_revision: str | Sequence[str] | None = "a8e2f6c10d49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("preferences", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("preferences")
