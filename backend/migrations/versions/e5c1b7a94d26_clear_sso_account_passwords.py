"""clear the passwords of SSO accounts

Revision ID: e5c1b7a94d26
Revises: e5c1b7a93d24
Create Date: 2026-11-15 12:00:00.000000

An admin could reset the password of an SSO account, and local sign-in
didn't check the account's provider, so that account could then sign in
with the password, without the identity provider or its MFA. Both are
refused now. This removes what such a reset left behind: the password hash
of every account whose auth_provider isn't local, its previous password
hashes, and a pending forced password change (an SSO account has no
password to change).

Each account cleared is recorded in the audit outbox (app/audit_outbox.py)
as a user.password_cleared with reason sso_account; the next start records
it in the audit log with no user. Nothing is written to auditlog here (see
c7f2a9e4d136).

Going down changes nothing: the hashes are not restored.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "e5c1b7a94d26"
down_revision: str | Sequence[str] | None = "e5c1b7a93d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_audit_outbox = sa.table(
    "audit_outbox",
    sa.column("action", sa.String),
    sa.column("metadata", sa.JSON),
    sa.column("created_at", sa.DateTime),
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            'SELECT id, username, auth_provider FROM "user" '
            "WHERE auth_provider != 'local' AND password_hash IS NOT NULL ORDER BY id"
        )
    ).fetchall()
    for user_id, username, auth_provider in rows:
        bind.execute(
            sa.text(
                'UPDATE "user" SET password_hash = NULL, password_changed_at = NULL, '
                "must_change_password = 0 WHERE id = :id"
            ),
            {"id": user_id},
        )
        bind.execute(sa.text("DELETE FROM password_history WHERE user_id = :id"), {"id": user_id})
        bind.execute(
            _audit_outbox.insert().values(
                action="user.password_cleared",
                metadata={
                    "reason": "sso_account",
                    "user_id": user_id,
                    "username": username,
                    "auth_provider": auth_provider,
                },
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )


def downgrade() -> None:
    pass
