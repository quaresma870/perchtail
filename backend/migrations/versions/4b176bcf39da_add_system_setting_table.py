"""add system_setting table

Revision ID: 4b176bcf39da
Revises: 8ff0150df75b
Create Date: 2026-08-05 23:43:40.532856

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '4b176bcf39da'
down_revision: Union[str, Sequence[str], None] = '8ff0150df75b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Autogenerate also proposed dropping the search_index_fts* shadow
    # tables -- it doesn't understand FTS5 virtual tables, so it sees their
    # SQLite-internal shadow tables as unmanaged and wants to remove them.
    # Stripped by hand; that index is very much still wanted (see the
    # 8ff0150df75b migration and app.db.ensure_search_schema).
    op.create_table('system_setting',
    sa.Column('key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('system_setting')
