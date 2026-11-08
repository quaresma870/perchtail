"""live alerts, no content index

Revision ID: a3f5c8e17b42
Revises: e4b7c2d91a05
Create Date: 2026-11-08 16:00:00.000000

The content index and its Search page go (#303): the FTS5 table, the
per-file index state, the per-source opt-in and the Search toggle are
dropped, so the space they took comes back. Alerts read new lines live
(#305): they gain a scope (folder or customer besides a source), a file
glob, regex matching and delivery state, positions move from line numbers
in the index to byte offsets in the files (existing alerts start again at
the end of each file), and failed webhook calls wait in alert_delivery to
be retried (#328).
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "a3f5c8e17b42"
down_revision: str | Sequence[str] | None = "e4b7c2d91a05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS search_index_fts")
    op.drop_table("search_index_state")
    with op.batch_alter_table("source") as batch_op:
        batch_op.drop_column("search_indexing_enabled")
    op.execute("DELETE FROM system_setting WHERE key = 'search_view_enabled'")

    with op.batch_alter_table("alert") as batch_op:
        batch_op.add_column(
            sa.Column("regex", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("folder_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("customer_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "file_glob",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="**/*.log",
            )
        )
        batch_op.add_column(sa.Column("last_fired_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("failed_deliveries", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.create_foreign_key("fk_alert_folder_id", "folder", ["folder_id"], ["id"])
        batch_op.create_foreign_key("fk_alert_customer_id", "customer", ["customer_id"], ["id"])

    # Line numbers in the index mean nothing for byte offsets in the files:
    # every alert starts again at the end of each file it watches.
    op.drop_table("alert_file_state")
    op.create_table(
        "alert_file_state",
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("head", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["alert_id"], ["alert.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("alert_id", "source_id", "file_path"),
    )
    op.create_table(
        "alert_delivery",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["alert.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alert_delivery_alert_id", "alert_delivery", ["alert_id"])


def downgrade() -> None:
    # The index content is gone for good; the tables come back empty.
    op.drop_index("ix_alert_delivery_alert_id", table_name="alert_delivery")
    op.drop_table("alert_delivery")
    op.drop_table("alert_file_state")
    op.create_table(
        "alert_file_state",
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("reported_through_line", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["alert.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("alert_id", "source_id", "file_path"),
    )
    with op.batch_alter_table("alert") as batch_op:
        batch_op.drop_constraint("fk_alert_customer_id", type_="foreignkey")
        batch_op.drop_constraint("fk_alert_folder_id", type_="foreignkey")
        for column in (
            "last_error",
            "failed_deliveries",
            "last_fired_at",
            "file_glob",
            "customer_id",
            "folder_id",
            "regex",
        ):
            batch_op.drop_column(column)
    with op.batch_alter_table("source") as batch_op:
        batch_op.add_column(
            sa.Column(
                "search_indexing_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
    op.create_table(
        "search_index_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "file_path", name="uq_search_index_state_source_path"),
    )
    op.execute(
        "CREATE VIRTUAL TABLE search_index_fts USING fts5("
        "source_id UNINDEXED, file_path, line_number UNINDEXED, snippet)"
    )
