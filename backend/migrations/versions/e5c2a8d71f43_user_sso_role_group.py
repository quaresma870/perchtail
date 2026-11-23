"""user sso role group

Revision ID: e5c2a8d71f43
Revises: e5c1b7a94d26
Create Date: 2026-11-15 10:00:00.000000

The identity-provider group whose mapping gave an SSO user their role at
their last sign-in (#384), shown in the access report. Null when no mapping
matched, when an admin changed the role by hand, and for local accounts.

Can be run again after an interruption (see _recover_interrupted_rebuild).
"""

import logging
import sys
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5c2a8d71f43"
down_revision: str | Sequence[str] | None = "e5c1b7a94d26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def _recover_interrupted_rebuild(table: str) -> None:
    """SQLite DDL isn't transactional: a batch rebuild runs CREATE TABLE
    _alembic_tmp_<table>, copies the rows into it, drops <table> and renames
    the copy, each step on its own, and an interruption can leave the
    temporary table behind. What it holds depends on where it stopped:

    - <table> still exists: the copy was never finished or never replaced
      it, so it is a leftover and is dropped;
    - only the copy exists: it stopped after the DROP, so the copy holds
      every row; the rebuild is finished by renaming it, and logged;
    - neither exists: nothing can be recovered here, so the migration stops
      and points at the pre-upgrade backup, without touching anything.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    temporary = f"_alembic_tmp_{table}"
    if inspector.has_table(table):
        if inspector.has_table(temporary):
            op.execute(sa.text(f'DROP TABLE "{temporary}"'))
        return
    if inspector.has_table(temporary):
        op.execute(sa.text(f'ALTER TABLE "{temporary}" RENAME TO "{table}"'))
        logger.warning(
            "An interrupted rebuild of table %s was finished: %s renamed back to %s.",
            table,
            temporary,
            table,
        )
        return
    logger.error(
        "Table %s is missing and there is no %s to recover it from. Restore the "
        "pre-upgrade backup (python -m app.backup restore) and run the upgrade again.",
        table,
        temporary,
    )
    sys.exit(1)


def _user_columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("user")}


def upgrade() -> None:
    _recover_interrupted_rebuild("user")
    if "sso_role_group" in _user_columns():
        return
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("sso_role_group", sa.String(), nullable=True))


def downgrade() -> None:
    _recover_interrupted_rebuild("user")
    if "sso_role_group" not in _user_columns():
        return
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("sso_role_group")
