"""MFA for every local account on new installs only

Revision ID: b4d8e2f61a93
Revises: c4e7a1d9b302
Create Date: 2026-11-26 01:00:00.000000

`require_mfa_local_accounts` now defaults to on (app/system_settings.py). An
existing install keeps what it had: when its database already has users and
no stored value, the old default (off) is stored. A new install runs this on
an empty database, with no users yet, and gets the new default. The same
pattern as the scheduled backup's default (f7a2c4e9b351).

It can run again after an interruption. Going down leaves the stored value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d8e2f61a93"
down_revision: str | Sequence[str] | None = "c4e7a1d9b302"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    has_users = bind.execute(sa.text('SELECT 1 FROM "user" LIMIT 1')).first() is not None
    stored = bind.execute(
        sa.text("SELECT 1 FROM system_setting WHERE key = 'require_mfa_local_accounts'")
    ).first()
    if has_users and stored is None:
        bind.execute(
            sa.text(
                "INSERT INTO system_setting (key, value) "
                "VALUES ('require_mfa_local_accounts', 'false')"
            )
        )


def downgrade() -> None:
    pass
