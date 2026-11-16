"""password reset token

Revision ID: b2e7c4a19f53
Revises: f7a2c4e9b351
Create Date: 2026-11-15 10:00:00.000000

Adds the password_reset_token table (app.auth.models.PasswordResetToken):
pending password-reset links for local accounts (#244), stored as the
SHA-256 of the token with fingerprints of the password hash and email they
were issued for. Empty until the feature is turned on and used.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "b2e7c4a19f53"
down_revision: str | Sequence[str] | None = "f7a2c4e9b351"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE = "password_reset_token"
# Index name -> (column, unique).
INDEXES = {
    "ix_password_reset_token_user_id": ("user_id", False),
    "ix_password_reset_token_token_hash": ("token_hash", True),
}


# SQLite DDL isn't transactional: an upgrade or downgrade stopped half way
# leaves some of these objects behind, so each step only runs when needed.
def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        _create_table()
    existing = {index["name"] for index in inspector.get_indexes(TABLE)}
    for name, (column, unique) in INDEXES.items():
        if name not in existing:
            op.create_index(name, TABLE, [column], unique=unique)


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("password_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("email_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return
    existing = {index["name"] for index in inspector.get_indexes(TABLE)}
    for name in reversed(INDEXES):
        if name in existing:
            op.drop_index(name, table_name=TABLE)
    op.drop_table(TABLE)
