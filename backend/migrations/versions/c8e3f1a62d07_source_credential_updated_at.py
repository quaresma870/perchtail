"""source credential updated at

Revision ID: c8e3f1a62d07
Revises: a3f5c8e17b42
Create Date: 2026-11-08 18:30:00.000000

When each source's credential was last set (#248), for its age in the
Sources list. Existing credentials have no date: their age is unknown until
they're next changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# The rebuild keeps AUTOINCREMENT (see 47c9d392f28f): ids of deleted sources
# are never handed out again.
NO_ID_REUSE = {"sqlite_autoincrement": True}

revision: str = "c8e3f1a62d07"
down_revision: str | Sequence[str] | None = "a3f5c8e17b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source", table_kwargs=NO_ID_REUSE) as batch_op:
        batch_op.add_column(sa.Column("credential_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("source", table_kwargs=NO_ID_REUSE) as batch_op:
        batch_op.drop_column("credential_updated_at")
