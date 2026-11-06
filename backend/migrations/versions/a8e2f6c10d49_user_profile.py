"""user profile

Revision ID: a8e2f6c10d49
Revises: f4c1d8e92a37
Create Date: 2026-11-06 17:00:00.000000

My profile (#243): an optional display name and email on each user. The
email is unique (stored lowercased). Existing users have neither until
they, an admin or their IdP set them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "a8e2f6c10d49"
down_revision: str | Sequence[str] | None = "f4c1d8e92a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(
            sa.Column("display_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(sa.Column("email", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_index(batch_op.f("ix_user_email"), ["email"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_email"))
        batch_op.drop_column("email")
        batch_op.drop_column("display_name")
