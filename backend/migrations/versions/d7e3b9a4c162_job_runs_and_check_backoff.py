"""job runs and check backoff

Revision ID: d7e3b9a4c162
Revises: a3f5c8e17b42
Create Date: 2026-11-08 20:00:00.000000

Each scheduled job's last run (#327): when it started and finished, whether
it worked and why not, so an overdue job runs soon after a restart. Each
source's background-check backoff (#343): consecutive failed checks and
until when they're skipped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "d7e3b9a4c162"
down_revision: str | Sequence[str] | None = "a3f5c8e17b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_run",
        sa.Column("job_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("last_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_ok", sa.Boolean(), nullable=True),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("last_duration_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
    )
    with op.batch_alter_table("source") as batch_op:
        batch_op.add_column(
            sa.Column("check_failures", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("check_backoff_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.drop_column("check_backoff_until")
        batch_op.drop_column("check_failures")
    op.drop_table("job_run")
