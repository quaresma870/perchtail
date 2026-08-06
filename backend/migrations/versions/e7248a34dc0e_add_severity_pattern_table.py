"""add severity_pattern table

Revision ID: e7248a34dc0e
Revises: 4b176bcf39da
Create Date: 2026-08-06 21:34:58.143381

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = "e7248a34dc0e"
down_revision: Union[str, Sequence[str], None] = "4b176bcf39da"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Autogenerate also proposed dropping the search_index_fts* shadow
    # tables -- it doesn't understand FTS5 virtual tables (see the
    # 4b176bcf39da migration for the same note). Stripped by hand; still
    # very much wanted (see app.db.ensure_search_schema).
    op.create_table(
        "severity_pattern",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column(
            "level",
            sa.Enum("error", "warning", "info", "debug", name="severitylevel"),
            nullable=False,
        ),
        sa.Column("pattern", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("pattern_kind", sa.Enum("glob", "regex", name="patternkind"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("highlight_line", sa.Boolean(), nullable=False),
        sa.Column("include_in_navigation", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("severity_pattern")
