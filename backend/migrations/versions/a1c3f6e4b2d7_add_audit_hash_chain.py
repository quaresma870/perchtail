"""add audit hash chain columns and audit_chain_state table

Revision ID: a1c3f6e4b2d7
Revises: 68895890bad7
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a1c3f6e4b2d7'
down_revision: Union[str, Sequence[str], None] = '68895890bad7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Both nullable: rows written before this feature existed have neither
    # until app.audit_hash_chain.backfill_chain_if_needed runs once at
    # startup (app/main.py's lifespan) -- deliberately not done as a data
    # migration here, since that logic is real business logic worth its own
    # unit tests, not something to bury in an Alembic script.
    op.add_column('auditlog', sa.Column('prev_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('auditlog', sa.Column('row_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_table(
        'audit_chain_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('anchor_row_id', sa.Integer(), nullable=True),
        sa.Column('anchor_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('broken_row_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('audit_chain_state')
    op.drop_column('auditlog', 'row_hash')
    op.drop_column('auditlog', 'prev_hash')
