"""winrm configuration

Revision ID: d9a4b2e61f08
Revises: c5e8a1f37b92
Create Date: 2026-11-07 10:00:00.000000

WinRM sources can name a JEA endpoint (PowerShell session configuration,
#169); null keeps the WinRS shell, as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "d9a4b2e61f08"
down_revision: str | Sequence[str] | None = "c5e8a1f37b92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.add_column(
            sa.Column("winrm_configuration", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.drop_column("winrm_configuration")
