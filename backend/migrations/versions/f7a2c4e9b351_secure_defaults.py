"""secure defaults: SMB signing and encryption per source, the scheduled
backup on new installs only, alert webhook URLs encrypted

Revision ID: f7a2c4e9b351
Revises: e5c2a8d71f43
Create Date: 2026-11-15 20:00:00.000000

- `source.smb_require_signing` and `source.smb_encrypt`. The SMB connector
  always asked for signing (smbprotocol's default), so existing sources get
  signing required, as before, and no encryption.
- The scheduled backup now defaults to every 24 hours (app/system_settings.py).
  An existing install keeps what it had: when its database already has users
  and no stored value, the old default (0, off) is stored. A new install runs
  this on an empty database, with no users yet, and gets the new default.
- `alert.webhook_url` now holds the URL Fernet-encrypted, like the other
  secrets (app/crypto.py); the values already there are encrypted here.
  With webhooks to encrypt (or, going down, to decrypt) and the credential
  salt missing, it stops without generating a new salt, as the app does.

Every step can run again after an interruption (CONTRIBUTING.md). Going down
decrypts the webhook URLs and drops the two columns; the stored backup
interval stays.
"""

import logging
import sys
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.engines import sqlite_only

revision: str = "f7a2c4e9b351"
down_revision: str | Sequence[str] | None = "e5c2a8d71f43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NO_ID_REUSE = {"sqlite_autoincrement": True}
_PLAIN_SCHEMES = ("http://", "https://")

logger = logging.getLogger("alembic.runtime.migration")


def _recover_interrupted_rebuild(table: str) -> None:
    """As in e5c1b7a93d24: a leftover _alembic_tmp_<table> next to <table> is
    dropped; one that stands alone holds every row and is renamed back."""
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


def _source_columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("source")}


def _source_sequence() -> int | None:
    if op.get_bind().dialect.name != "sqlite":
        return None
    return (
        op.get_bind()
        .execute(sa.text("SELECT seq FROM sqlite_sequence WHERE name = 'source'"))
        .scalar()
    )


def _restore_source_sequence(seq: int | None) -> None:
    """The rebuild drops the old table and with it its sqlite_sequence row,
    so ids of sources deleted before it would be handed out again."""
    if seq is None:
        return
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'source'"))
    bind.execute(
        sa.text(
            "INSERT INTO sqlite_sequence (name, seq) "
            "VALUES ('source', MAX(:seq, (SELECT COALESCE(MAX(id), 0) FROM source)))"
        ),
        {"seq": seq},
    )


def _assert_credential_salt_present() -> None:
    """Called only before webhook URLs are encrypted or decrypted, which
    derives the key: app/crypto.py creates a new salt when the file is
    missing, and this runs before the app's own check (app/bootstrap.py,
    assert_credential_salt_present, which also counts alerts). It refuses
    in the same way instead of generating one, so a lost salt stays
    recoverable. With no webhook to change, nothing here touches the salt
    and the app's check decides at startup."""
    from pathlib import Path

    from app.bootstrap import CredentialSaltMissingError, credential_salt_missing_message
    from app.config import get_settings

    settings = get_settings()
    salt_path = Path(settings.credential_salt_path)
    if salt_path.exists() or settings.credential_salt_allow_regenerate:
        return
    message = credential_salt_missing_message(salt_path)
    logger.error(message)
    raise CredentialSaltMissingError(message)


def _plain_webhooks() -> list[tuple[int, str]]:
    rows = op.get_bind().execute(sa.text("SELECT id, webhook_url FROM alert")).all()
    return [(row.id, row.webhook_url) for row in rows if row.webhook_url.startswith(_PLAIN_SCHEMES)]


def upgrade() -> None:
    # Before any change, so a refusal leaves the database as it was.
    if not sqlite_only():
        return
    if _plain_webhooks():
        _assert_credential_salt_present()
    _recover_interrupted_rebuild("source")
    columns = _source_columns()
    if "smb_require_signing" not in columns:
        op.add_column(
            "source",
            sa.Column(
                "smb_require_signing", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
        )
    if "smb_encrypt" not in columns:
        op.add_column(
            "source",
            sa.Column("smb_encrypt", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    bind = op.get_bind()
    has_users = bind.execute(sa.text('SELECT 1 FROM "user" LIMIT 1')).first() is not None
    stored = bind.execute(
        sa.text("SELECT 1 FROM system_setting WHERE key = 'backup_interval_hours'")
    ).first()
    if has_users and stored is None:
        bind.execute(
            sa.text("INSERT INTO system_setting (key, value) VALUES ('backup_interval_hours', '0')")
        )

    plain = _plain_webhooks()
    if plain:
        from app.crypto import encrypt_secret

        for alert_id, url in plain:
            bind.execute(
                sa.text("UPDATE alert SET webhook_url = :value WHERE id = :id"),
                {"value": encrypt_secret(url), "id": alert_id},
            )


def downgrade() -> None:
    if not sqlite_only():
        return
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, webhook_url FROM alert")).all()
    encrypted = [row for row in rows if not row.webhook_url.startswith(_PLAIN_SCHEMES)]
    if encrypted:
        _assert_credential_salt_present()
        from app.crypto import decrypt_secret

        for row in encrypted:
            bind.execute(
                sa.text("UPDATE alert SET webhook_url = :value WHERE id = :id"),
                {"value": decrypt_secret(row.webhook_url), "id": row.id},
            )

    _recover_interrupted_rebuild("source")
    columns = _source_columns()
    if not {"smb_require_signing", "smb_encrypt"} & columns:
        return
    seq = _source_sequence()
    with op.batch_alter_table("source", recreate="always", table_kwargs=NO_ID_REUSE) as batch:
        for name in ("smb_encrypt", "smb_require_signing"):
            if name in columns:
                batch.drop_column(name)
    _restore_source_sequence(seq)
