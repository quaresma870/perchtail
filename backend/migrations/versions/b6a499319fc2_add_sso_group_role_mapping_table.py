"""add sso_group_role_mapping table

Revision ID: b6a499319fc2
Revises: e7248a34dc0e
Create Date: 2026-08-17 17:52:46.570408

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = "b6a499319fc2"
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
        "sso_group_role_mapping",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("group_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("sso_group_role_mapping")
