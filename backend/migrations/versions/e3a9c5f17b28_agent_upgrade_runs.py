"""agent upgrade runs

Revision ID: e3a9c5f17b28
Revises: f2b8d4c61e93
Create Date: 2026-11-11 21:00:00.000000

Upgrading push agents from the portal (#319): every attempt is stored in
agent_upgrade_run, and each source gets its own upgrade policy ("inherit"
follows Settings -> Agents) and its count of consecutive failed upgrades.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# The rebuild keeps AUTOINCREMENT (see 47c9d392f28f): ids of deleted sources
# are never handed out again.
NO_ID_REUSE = {"sqlite_autoincrement": True}

revision: str = "e3a9c5f17b28"
down_revision: str | Sequence[str] | None = "f2b8d4c61e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source", table_kwargs=NO_ID_REUSE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "agent_upgrade_policy",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="inherit",
            )
        )
        batch_op.add_column(
            sa.Column("agent_upgrade_failures", sa.Integer(), nullable=False, server_default="0")
        )
    op.create_table(
        "agent_upgrade_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("command_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("from_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("to_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("trigger", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("suspended_source", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("agent_upgrade_run") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_agent_upgrade_run_source_id"), ["source_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_agent_upgrade_run_command_id"), ["command_id"], unique=True
        )
        batch_op.create_index(batch_op.f("ix_agent_upgrade_run_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_upgrade_run") as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_upgrade_run_status"))
        batch_op.drop_index(batch_op.f("ix_agent_upgrade_run_command_id"))
        batch_op.drop_index(batch_op.f("ix_agent_upgrade_run_source_id"))
    op.drop_table("agent_upgrade_run")
    with op.batch_alter_table("source", table_kwargs=NO_ID_REUSE) as batch_op:
        batch_op.drop_column("agent_upgrade_failures")
        batch_op.drop_column("agent_upgrade_policy")
