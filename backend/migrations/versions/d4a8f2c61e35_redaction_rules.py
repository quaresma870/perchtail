"""redaction rules

Revision ID: d4a8f2c61e35
Revises: a8d3f5c27e14
Create Date: 2026-11-14 18:00:00.000000

Redaction (#382, #249, app/redaction_rules.py): the redaction_rule table
(system rules with a null source_id, and each source's own rules), with
AUTOINCREMENT so an override never points at a reused id, and the
source_redaction_override table (a source turning a system rule off). The
built-in rules are seeded, disabled, at the next start.

Clears alert_file_state.head: the hash of a file's first bytes is becoming
an HMAC with a per-source key (app.redaction.head_digest), which no stored
head would match, and a mismatch reads the file again from the start and
repeats its alerts. A null head is never compared; the next pass stores the
new one. Offsets are kept.

Can be run again after an interruption: SQLite DDL isn't transactional, so
each table and index is created only when it isn't there yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from migrations.engines import sqlite_only

revision: str = "d4a8f2c61e35"
down_revision: str | Sequence[str] | None = "a8d3f5c27e14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX = "ix_redaction_rule_source_id"


def upgrade() -> None:
    if not sqlite_only():
        return
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("redaction_rule"):
        _create_redaction_rule()
    if INDEX not in {index["name"] for index in inspector.get_indexes("redaction_rule")}:
        op.create_index(INDEX, "redaction_rule", ["source_id"], unique=False)
    if not inspector.has_table("source_redaction_override"):
        _create_source_redaction_override()
    op.execute("UPDATE alert_file_state SET head = NULL")


def _create_redaction_rule() -> None:
    op.create_table(
        "redaction_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("detector", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("pattern", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("secret_group", sa.Integer(), nullable=True),
        sa.Column("max_length", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )


def _create_source_redaction_override() -> None:
    op.create_table(
        "source_redaction_override",
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["rule_id"], ["redaction_rule.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("source_id", "rule_id"),
    )


def downgrade() -> None:
    if not sqlite_only():
        return
    op.drop_table("source_redaction_override")
    with op.batch_alter_table("redaction_rule") as batch_op:
        batch_op.drop_index(batch_op.f("ix_redaction_rule_source_id"))
    op.drop_table("redaction_rule")
