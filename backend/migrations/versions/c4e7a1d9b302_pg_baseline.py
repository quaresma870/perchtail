"""pg baseline

Revision ID: c4e7a1d9b302
Revises: a7c3e9d2f514
Create Date: 2026-11-26 12:00:00.000000

PostgreSQL support (#306). A PostgreSQL database starts empty: the revisions
before this one were written for SQLite and do nothing on PostgreSQL
(migrations/engines.py, sqlite_only). This one creates there the whole schema
of a7c3e9d2f514, frozen as it was then (it doesn't follow the models), and
does nothing on SQLite, which already has it. The revisions after this one
run on both engines.

On PostgreSQL:
- enum columns are VARCHAR (the models use native_enum=False), so a new value
  never needs ALTER TYPE;
- file_name_entry.size and alert_file_state.offset are BIGINT (a file can be
  bigger than 2 GiB);
- file_name_entry.path gets a trigram GIN index from the pg_trgm extension
  (trusted: the database's owner may create it), which replaces SQLite's FTS5
  table (app/name_index_store.py);
- the update_check_state row is created, as on SQLite.

Going down drops every table this created; the extension is left, since it
may have been there before. There is nothing below this on PostgreSQL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from migrations.engines import postgresql_only

revision: str = "c4e7a1d9b302"
down_revision: str | Sequence[str] | None = "a7c3e9d2f514"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not postgresql_only():
        return
    op.create_table(
        "audit_chain_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("anchor_row_id", sa.Integer(), nullable=True),
        sa.Column("anchor_hash", sa.String(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("broken_row_id", sa.Integer(), nullable=True),
        sa.Column("known_break_cutoff_row_id", sa.Integer(), nullable=True),
        sa.Column("race_break_count", sa.Integer(), nullable=False),
        sa.Column("known_break_count", sa.Integer(), nullable=False),
        sa.Column("known_breaks_recorded_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "customer",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_customer_name"), "customer", ["name"], unique=True)
    op.create_table(
        "job_run",
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("last_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_ok", sa.Boolean(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_duration_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_table(
        "monitoring_token",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "role",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("is_super_admin", sa.Boolean(), nullable=False),
        sa.Column("global_capabilities", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_role_name"), "role", ["name"], unique=True)
    op.create_table(
        "smtp_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("security", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("password_encrypted", sa.String(), nullable=True),
        sa.Column("from_address", sa.String(), nullable=False),
        sa.Column("from_name", sa.String(), nullable=True),
        sa.Column("tls_ca_pem", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ssoproviderconfig",
        sa.Column("id", sa.Integer(), nullable=False),
        # SSOProtocol: oidc, saml.
        sa.Column("protocol", sa.String(length=4), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("config", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "system_setting",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "update_check_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(), nullable=True),
        sa.Column("latest_version", sa.String(), nullable=True),
        sa.Column("latest_name", sa.String(), nullable=True),
        sa.Column("latest_url", sa.String(), nullable=True),
        sa.Column("latest_published_at", sa.DateTime(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("notified_version", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # The singleton row (app/update_check.py's STATE_ID), as e7a3c9d24b15
    # creates it on SQLite, so the daily job and a "Check now" never race to
    # insert it.
    op.execute("INSERT INTO update_check_state (id) VALUES (1)")
    op.create_table(
        "folder",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("parent_folder_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customer.id"]),
        sa.ForeignKeyConstraint(["parent_folder_id"], ["folder.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "rolegrant",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        # ScopeType: customer, folder, source.
        sa.Column("scope_type", sa.String(length=8), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_id", "scope_type", "scope_id", name="uq_rolegrant_scope"),
    )
    op.create_table(
        "sso_group_role_mapping",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("group_name", sa.String(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("preferences", sa.JSON(), nullable=True),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        # AuthProviderType: local, oidc, saml.
        sa.Column("auth_provider", sa.String(length=5), nullable=False),
        sa.Column("external_issuer", sa.String(), nullable=True),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("sso_role_group", sa.String(), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False),
        sa.Column("mfa_secret_encrypted", sa.String(), nullable=True),
        sa.Column("mfa_enrolled_at", sa.DateTime(), nullable=True),
        sa.Column("mfa_last_used_step", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_email"), "user", ["email"], unique=True)
    op.create_index(op.f("ix_user_external_issuer"), "user", ["external_issuer"], unique=False)
    op.create_index(op.f("ix_user_username"), "user", ["username"], unique=True)
    op.create_table(
        "auditlog",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("prev_hash", sa.String(), nullable=True),
        sa.Column("row_hash", sa.String(), nullable=True),
        sa.Column("client_ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("hash_format", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "authsession",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_authsession_token_hash"), "authsession", ["token_hash"], unique=True)
    op.create_table(
        "mfa_backup_code",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mfa_backup_code_user_id"), "mfa_backup_code", ["user_id"], unique=False
    )
    op.create_table(
        "password_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_password_history_user_id"), "password_history", ["user_id"], unique=False
    )
    op.create_table(
        "password_reset_token",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("password_fingerprint", sa.String(), nullable=False),
        sa.Column("email_fingerprint", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_password_reset_token_token_hash"),
        "password_reset_token",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_password_reset_token_user_id"), "password_reset_token", ["user_id"], unique=False
    )
    op.create_table(
        "source",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("folder_id", sa.Integer(), nullable=True),
        # Protocol: ssh, smb, winrm, local, agent.
        sa.Column("protocol", sa.String(length=5), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("credential_ref", sa.String(), nullable=True),
        sa.Column("credential_updated_at", sa.DateTime(), nullable=True),
        sa.Column("base_path", sa.String(), nullable=False),
        sa.Column("roots", sa.JSON(), nullable=True),
        sa.Column("winrm_scheme", sa.String(), nullable=True),
        sa.Column("winrm_configuration", sa.String(), nullable=True),
        sa.Column("tls_ca_pem", sa.String(), nullable=True),
        sa.Column("smb_require_signing", sa.Boolean(), nullable=False),
        sa.Column("smb_encrypt", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("agent_token_hash", sa.String(), nullable=True),
        sa.Column("agent_last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("agent_os", sa.String(), nullable=True),
        sa.Column("agent_version", sa.String(), nullable=True),
        sa.Column("agent_features", sa.String(), nullable=True),
        sa.Column(
            "agent_upgrade_policy", sa.String(), server_default="inherit", nullable=False
        ),
        sa.Column("agent_upgrade_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("status_checked_at", sa.DateTime(), nullable=True),
        sa.Column("status_message", sa.String(), nullable=True),
        sa.Column("check_failures", sa.Integer(), nullable=False),
        sa.Column("check_backoff_until", sa.DateTime(), nullable=True),
        sa.Column("name_index_enabled", sa.Boolean(), nullable=False),
        sa.Column("name_indexed_at", sa.DateTime(), nullable=True),
        sa.Column("name_index_files", sa.Integer(), nullable=True),
        sa.Column("name_index_message", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customer.id"]),
        sa.ForeignKeyConstraint(["folder_id"], ["folder.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent_upgrade_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("command_id", sa.String(), nullable=False),
        sa.Column("from_version", sa.String(), nullable=True),
        sa.Column("to_version", sa.String(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=True),
        sa.Column("suspended_source", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_upgrade_run_command_id"), "agent_upgrade_run", ["command_id"], unique=True
    )
    op.create_index(
        op.f("ix_agent_upgrade_run_source_id"), "agent_upgrade_run", ["source_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_upgrade_run_status"), "agent_upgrade_run", ["status"], unique=False
    )
    op.create_table(
        "alert",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("query", sa.String(), nullable=False),
        sa.Column("regex", sa.Boolean(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("folder_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("file_glob", sa.String(), nullable=False),
        sa.Column("webhook_url", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(), nullable=True),
        sa.Column("failed_deliveries", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customer.id"]),
        sa.ForeignKeyConstraint(["folder_id"], ["folder.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "file_name_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "path", name="uq_file_name_entry_source_path"),
    )
    op.create_index(
        op.f("ix_file_name_entry_source_id"), "file_name_entry", ["source_id"], unique=False
    )
    # The file-name index's substring search (app/name_index_store.py,
    # ensure_name_index_schema does the same at startup).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_file_name_entry_path_trgm "
        "ON file_name_entry USING gin (path gin_trgm_ops)"
    )
    op.create_table(
        "redaction_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("detector", sa.String(), nullable=True),
        sa.Column("pattern", sa.String(), nullable=True),
        sa.Column("secret_group", sa.Integer(), nullable=True),
        sa.Column("max_length", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_redaction_rule_source_id"), "redaction_rule", ["source_id"], unique=False
    )
    op.create_table(
        "rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        # RuleType: include, exclude.
        sa.Column("type", sa.String(length=7), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        # PatternKind: glob, regex.
        sa.Column("pattern_kind", sa.String(length=5), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "severity_pattern",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        # SeverityLevel: error, warning, info, debug.
        sa.Column("level", sa.String(length=7), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        # PatternKind: glob, regex.
        sa.Column("pattern_kind", sa.String(length=5), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("highlight_line", sa.Boolean(), nullable=False),
        sa.Column("include_in_navigation", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "alert_delivery",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["alert.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_alert_delivery_alert_id"), "alert_delivery", ["alert_id"], unique=False
    )
    op.create_table(
        "alert_file_state",
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False),
        sa.Column("head", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["alert_id"], ["alert.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("alert_id", "source_id", "file_path"),
    )
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
    if not postgresql_only():
        return
    # Each table's indexes go with it; the referencing tables go first.
    for table in (
        "source_redaction_override",
        "alert_file_state",
        "alert_delivery",
        "severity_pattern",
        "rule",
        "redaction_rule",
        "file_name_entry",
        "alert",
        "agent_upgrade_run",
        "source",
        "password_reset_token",
        "password_history",
        "mfa_backup_code",
        "authsession",
        "auditlog",
        "user",
        "sso_group_role_mapping",
        "rolegrant",
        "folder",
        "update_check_state",
        "system_setting",
        "ssoproviderconfig",
        "smtp_settings",
        "role",
        "monitoring_token",
        "job_run",
        "customer",
        "audit_outbox",
        "audit_chain_state",
    ):
        op.drop_table(table)
