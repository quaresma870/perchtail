"""merge monitoring token and alert table heads

Revision ID: b87908ef272f
Revises: 9446b13e0f2f, 9dcd8aac8bc4
Create Date: 2026-08-21 11:20:11.911353

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b87908ef272f'
down_revision: Union[str, Sequence[str], None] = ('9446b13e0f2f', '9dcd8aac8bc4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
