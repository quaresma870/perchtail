"""file name index

Revision ID: e4b7c2d91a05
Revises: d9a4b2e61f08
Create Date: 2026-11-07 23:00:00.000000

The file-name index (#304): one row per rule-visible file of a source
(path, size, modification time, never content), an FTS5 table with the
trigram tokenizer over the paths, kept in step by triggers, and per-source
state (on by default, last pass, file count, why the last pass stopped).
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# The rebuild keeps AUTOINCREMENT (see 47c9d392f28f): ids of deleted sources
# are never handed out again.
NO_ID_REUSE = {"sqlite_autoincrement": True}

revision: str = "e4b7c2d91a05"
down_revision: str | Sequence[str] | None = "d9a4b2e61f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source", table_kwargs=NO_ID_REUSE) as batch_op:
        batch_op.add_column(
            sa.Column("name_index_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(sa.Column("name_indexed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("name_index_files", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("name_index_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )

    op.create_table(
        "file_name_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "path", name="uq_file_name_entry_source_path"),
    )
    op.create_index("ix_file_name_entry_source_id", "file_name_entry", ["source_id"])

    # Same DDL as app/name_index_store.ensure_name_index_schema, inlined so
    # this migration doesn't change if that module does.
    op.execute(
        "CREATE VIRTUAL TABLE file_name_fts USING fts5("
        "path, content='file_name_entry', content_rowid='id', tokenize='trigram')"
    )
    op.execute(
        "CREATE TRIGGER file_name_entry_ai AFTER INSERT ON file_name_entry BEGIN "
        "INSERT INTO file_name_fts(rowid, path) VALUES (new.id, new.path); END"
    )
    op.execute(
        "CREATE TRIGGER file_name_entry_ad AFTER DELETE ON file_name_entry BEGIN "
        "INSERT INTO file_name_fts(file_name_fts, rowid, path) VALUES ('delete', old.id, old.path); END"
    )
    op.execute(
        "CREATE TRIGGER file_name_entry_au AFTER UPDATE OF path ON file_name_entry BEGIN "
        "INSERT INTO file_name_fts(file_name_fts, rowid, path) VALUES ('delete', old.id, old.path); "
        "INSERT INTO file_name_fts(rowid, path) VALUES (new.id, new.path); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS file_name_entry_au")
    op.execute("DROP TRIGGER IF EXISTS file_name_entry_ad")
    op.execute("DROP TRIGGER IF EXISTS file_name_entry_ai")
    op.execute("DROP TABLE IF EXISTS file_name_fts")
    op.drop_index("ix_file_name_entry_source_id", table_name="file_name_entry")
    op.drop_table("file_name_entry")
    with op.batch_alter_table("source", table_kwargs=NO_ID_REUSE) as batch_op:
        batch_op.drop_column("name_index_message")
        batch_op.drop_column("name_index_files")
        batch_op.drop_column("name_indexed_at")
        batch_op.drop_column("name_index_enabled")
