"""manage_source capability

Revision ID: b9e4d7a2c513
Revises: e3a9c5f17b28
Create Date: 2026-11-13 22:00:00.000000

Changing existing sources and folders moves from manage_rules to its own
scoped capability, manage_source. To keep what every role could do, each
grant that has manage_rules, in a role that has the create_source global
capability, also gets manage_source. Roles without create_source couldn't
change sources before, so they get nothing new. Going down removes
manage_source from every grant, since older versions don't know it.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9e4d7a2c513"
down_revision: str | Sequence[str] | None = "e3a9c5f17b28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return list(value or [])


def upgrade() -> None:
    conn = op.get_bind()
    roles = conn.execute(sa.text("SELECT id, global_capabilities FROM role")).fetchall()
    creators = {
        role_id for role_id, caps in roles if "create_source" in _as_list(caps)
    }
    if not creators:
        return
    grants = conn.execute(sa.text("SELECT id, role_id, capabilities FROM rolegrant")).fetchall()
    for grant_id, role_id, caps in grants:
        capabilities = _as_list(caps)
        if role_id not in creators or "manage_rules" not in capabilities:
            continue
        if "manage_source" in capabilities:
            continue
        capabilities.append("manage_source")
        conn.execute(
            sa.text("UPDATE rolegrant SET capabilities = :caps WHERE id = :id"),
            {"caps": json.dumps(capabilities), "id": grant_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    grants = conn.execute(sa.text("SELECT id, capabilities FROM rolegrant")).fetchall()
    for grant_id, caps in grants:
        capabilities = _as_list(caps)
        if "manage_source" not in capabilities:
            continue
        capabilities = [c for c in capabilities if c != "manage_source"]
        conn.execute(
            sa.text("UPDATE rolegrant SET capabilities = :caps WHERE id = :id"),
            {"caps": json.dumps(capabilities), "id": grant_id},
        )
