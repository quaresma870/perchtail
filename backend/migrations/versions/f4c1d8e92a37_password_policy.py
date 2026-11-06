"""password policy

Revision ID: f4c1d8e92a37
Revises: e7a3c9d24b15
Create Date: 2026-11-06 15:00:00.000000

Password policy (#245): when each local password was last set, for the
optional expiry, and the history of previous hashes for the optional
no-reuse rule. Existing local passwords count as set at the upgrade, so
turning expiry on doesn't lock everyone out at once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "f4c1d8e92a37"
down_revision: str | Sequence[str] | None = "e7a3c9d24b15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("password_changed_at", sa.DateTime(), nullable=True))
    op.execute(
        'UPDATE "user" SET password_changed_at = CURRENT_TIMESTAMP WHERE password_hash IS NOT NULL'
    )
    op.create_table(
        "password_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("password_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_password_history_user_id"), "password_history", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_password_history_user_id"), table_name="password_history")
    op.drop_table("password_history")
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("password_changed_at")
