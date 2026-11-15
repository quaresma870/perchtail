"""one grant per role and scope

Revision ID: a8d3f5c27e14
Revises: d4a8f2c6b913
Create Date: 2026-11-14 18:00:00.000000

A role could hold two grants for the same scope (scope_type, scope_id), and
grant resolution read only the first one it found. This keeps, for each
role and scope, the grant with the lowest id (the one resolution used),
deletes the others, and adds the unique constraint uq_rolegrant_scope.

Each deleted grant is recorded in the audit outbox (app/audit_outbox.py) as
a role_grant.delete with reason duplicate_scope, the grant kept and the
capabilities deleted; the next start records it in the audit log with no
user. Nothing is written to auditlog here (see c7f2a9e4d136).

Going down drops the constraint; the deleted grants are not restored.

Can be run again after an interruption (see _recover_interrupted_rebuild);
the rebuild is skipped when the constraint is already there (or, going down,
already gone).
"""

import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "a8d3f5c27e14"
down_revision: str | Sequence[str] | None = "d4a8f2c6b913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "uq_rolegrant_scope"

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


def _has_constraint() -> bool:
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("rolegrant")
    return any(constraint["name"] == CONSTRAINT for constraint in constraints)

_audit_outbox = sa.table(
    "audit_outbox",
    sa.column("action", sa.String),
    sa.column("metadata", sa.JSON),
    sa.column("created_at", sa.DateTime),
)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return list(value or [])


def upgrade() -> None:
    _recover_interrupted_rebuild("rolegrant")
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, role_id, scope_type, scope_id, capabilities FROM rolegrant ORDER BY id")
    ).fetchall()
    kept: dict[tuple, int] = {}
    for grant_id, role_id, scope_type, scope_id, capabilities in rows:
        key = (role_id, scope_type, scope_id)
        if key not in kept:
            kept[key] = grant_id
            continue
        bind.execute(sa.text("DELETE FROM rolegrant WHERE id = :id"), {"id": grant_id})
        bind.execute(
            _audit_outbox.insert().values(
                action="role_grant.delete",
                metadata={
                    "reason": "duplicate_scope",
                    "grant_id": grant_id,
                    "kept_grant_id": kept[key],
                    "role_id": role_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "capabilities": [str(c) for c in _as_list(capabilities)],
                },
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )

    if _has_constraint():
        return
    with op.batch_alter_table("rolegrant") as batch:
        batch.create_unique_constraint(CONSTRAINT, ["role_id", "scope_type", "scope_id"])


def downgrade() -> None:
    _recover_interrupted_rebuild("rolegrant")
    if not _has_constraint():
        return
    with op.batch_alter_table("rolegrant") as batch:
        batch.drop_constraint(CONSTRAINT, type_="unique")
