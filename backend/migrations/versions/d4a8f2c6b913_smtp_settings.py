"""smtp settings

Revision ID: d4a8f2c6b913
Revises: c7f2a9e4d136
Create Date: 2026-11-14 18:00:00.000000

Adds the smtp_settings table (app.models.SmtpSettings): the system SMTP
server a super admin sets from Settings -> Email, with its password
Fernet-encrypted. Empty until configured, so nothing is sent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from migrations.engines import sqlite_only

revision: str = "d4a8f2c6b913"
down_revision: str | Sequence[str] | None = "c7f2a9e4d136"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not sqlite_only():
        return
    op.create_table(
        "smtp_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("security", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("username", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("password_encrypted", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("from_address", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("from_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("tls_ca_pem", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    if not sqlite_only():
        return
    op.drop_table("smtp_settings")
