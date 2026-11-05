"""winrm scheme and per-source CA

Revision ID: b7d4f2a9c610
Revises: a4c2e8f1b937
Create Date: 2026-11-04 05:00:00.000000

An explicit HTTPS/HTTP choice for WinRM sources and a per-source CA
certificate (#169). Both null for existing sources, which keep inferring
the scheme from the port.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "b7d4f2a9c610"
down_revision: str | Sequence[str] | None = "a4c2e8f1b937"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.add_column(
            sa.Column("winrm_scheme", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("tls_ca_pem", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("source") as batch_op:
        batch_op.drop_column("tls_ca_pem")
        batch_op.drop_column("winrm_scheme")
