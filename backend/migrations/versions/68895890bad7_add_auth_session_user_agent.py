"""add auth_session.user_agent

Revision ID: 68895890bad7
Revises: 119064aae7d5
Create Date: 2026-08-25 20:00:56.499733

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '68895890bad7'
down_revision: Union[str, Sequence[str], None] = '119064aae7d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Autogenerate also proposed dropping the search_index_fts* tables --
    # those are FTS5 virtual tables created via raw SQL in app.db's
    # ensure_search_schema(), not part of SQLModel.metadata, so autogenerate
    # always sees them as "not in metadata" and wants them gone. Same false
    # positive every migration touching this DB hits; stripped here as in
    # every prior one.
    op.add_column(
        'authsession', sa.Column('user_agent', sqlmodel.sql.sqltypes.AutoString(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('authsession', 'user_agent')
