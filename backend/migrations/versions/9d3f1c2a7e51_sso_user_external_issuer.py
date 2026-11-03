"""sso user external_issuer

Revision ID: 9d3f1c2a7e51
Revises: c5e1a7d93b20
Create Date: 2026-11-03 05:00:00.000000

SSO accounts were matched on the IdP's `sub` claim alone, but `sub` is only
unique within one issuer. This adds the issuer to the account's identity.
Existing SSO accounts are bound to the configured provider's issuer at
startup (app.bootstrap.backfill_sso_user_issuers): that needs the credential
encryption key to read the provider config, which a migration doesn't have.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "9d3f1c2a7e51"
down_revision: str | Sequence[str] | None = "c5e1a7d93b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(
            sa.Column("external_issuer", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.create_index("ix_user_external_issuer", ["external_issuer"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_index("ix_user_external_issuer")
        batch_op.drop_column("external_issuer")
