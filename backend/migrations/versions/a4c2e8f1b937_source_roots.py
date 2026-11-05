"""source roots

Revision ID: a4c2e8f1b937
Revises: 9d3f1c2a7e51
Create Date: 2026-11-04 04:00:00.000000

Lets one source read several folders (#161). Null for every existing
source, which keeps reading its single base_path.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c2e8f1b937"
down_revision: str | Sequence[str] | None = "9d3f1c2a7e51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.add_column(sa.Column("roots", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.drop_column("roots")
