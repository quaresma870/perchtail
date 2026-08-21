"""add monitoring_token table

Revision ID: 9446b13e0f2f
Revises: e7248a34dc0e
Create Date: 2026-08-17 17:03:50.425531

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = "9446b13e0f2f"
down_revision: Union[str, Sequence[str], None] = "e7248a34dc0e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Autogenerate also proposed dropping the search_index_fts* shadow
    # tables -- it doesn't understand FTS5 virtual tables (see the
    # 4b176bcf39da migration for the same note). Stripped by hand; still
    # very much wanted (see app.db.ensure_search_schema).
    op.create_table(
        "monitoring_token",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("monitoring_token")
