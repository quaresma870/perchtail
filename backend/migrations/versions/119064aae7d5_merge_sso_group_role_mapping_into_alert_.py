"""merge sso group role mapping into alert+monitoring merge

Revision ID: 119064aae7d5
Revises: b6a499319fc2, b87908ef272f
Create Date: 2026-08-21 11:22:51.397904

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '119064aae7d5'
down_revision: Union[str, Sequence[str], None] = ('b6a499319fc2', 'b87908ef272f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
