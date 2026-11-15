"""Every migration that rebuilds a table must keep AUTOINCREMENT on the
tables whose ids must never be reused (see app.models.NO_ID_REUSE and
migration 47c9d392f28f): a rebuild through Alembic's batch mode drops it
unless it's asked for, as e4b7c2d91a05 once did for `source`."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
NO_ID_REUSE_TABLES = ("customer", "folder", "source", "role", "auditlog")


def _autoincrement_tables(db: Path) -> dict[str, bool]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN "
            f"({','.join('?' * len(NO_ID_REUSE_TABLES))})",
            NO_ID_REUSE_TABLES,
        ).fetchall()
    return {name: "AUTOINCREMENT" in (sql or "").upper() for name, sql in rows}


def _alembic(data_dir: Path, *args: str) -> None:
    env = {**os.environ, "DATA_DIR": str(data_dir)}
    env.pop("DATABASE_URL", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
    )


def test_a_fresh_upgrade_keeps_autoincrement(tmp_path):
    _alembic(tmp_path, "upgrade", "head")
    assert _autoincrement_tables(tmp_path / "perchtail.db") == dict.fromkeys(
        NO_ID_REUSE_TABLES, True
    )


def test_the_repair_restores_it_and_the_sequence(tmp_path):
    # A database that ran the file-name index migration while it still
    # dropped AUTOINCREMENT: simulate it by rebuilding `source` without it.
    _alembic(tmp_path, "upgrade", "c8e3f1a62d07")
    db = tmp_path / "perchtail.db"
    with sqlite3.connect(db) as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'source'").fetchone()[0]
        conn.execute("ALTER TABLE source RENAME TO source_old")
        conn.execute(sql.replace("AUTOINCREMENT", ""))
        conn.execute("INSERT INTO source SELECT * FROM source_old")
        conn.execute("DROP TABLE source_old")
        conn.execute(
            "INSERT INTO auditlog (action, target_type, target_id, timestamp) "
            "VALUES ('source.delete', 'source', 41, '2026-11-08 00:00:00')"
        )
    assert _autoincrement_tables(db)["source"] is False

    _alembic(tmp_path, "upgrade", "head")
    assert _autoincrement_tables(db)["source"] is True
    with sqlite3.connect(db) as conn:
        seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'source'").fetchone()[0]
    assert seq >= 41


def test_the_agent_upgrade_migration_goes_down_and_up(tmp_path):
    # e3a9c5f17b28 (#319): the source columns and agent_upgrade_run come and
    # go, and the source table keeps AUTOINCREMENT both ways.
    db = tmp_path / "perchtail.db"
    _alembic(tmp_path, "upgrade", "head")

    def columns(table):
        with sqlite3.connect(db) as conn:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    assert {"agent_upgrade_policy", "agent_upgrade_failures"} <= columns("source")
    assert "status" in columns("agent_upgrade_run")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO source (name, protocol, host, base_path, enabled, is_system, "
            "check_failures, name_index_enabled) VALUES ('a', 'agent', 'h', '/', 1, 0, 0, 1)"
        )
        policy, failures = conn.execute(
            "SELECT agent_upgrade_policy, agent_upgrade_failures FROM source"
        ).fetchone()
    assert (policy, failures) == ("inherit", 0)

    _alembic(tmp_path, "downgrade", "f2b8d4c61e93")
    assert not {"agent_upgrade_policy", "agent_upgrade_failures"} & columns("source")
    assert columns("agent_upgrade_run") == set()
    assert _autoincrement_tables(db)["source"] is True

    _alembic(tmp_path, "upgrade", "head")
    assert "agent_upgrade_policy" in columns("source")
    assert _autoincrement_tables(db)["source"] is True


def test_the_manage_source_migration_keeps_what_each_role_could_do(tmp_path):
    # b9e4d7a2c513: changing sources moved from manage_rules to manage_source.
    # A role with create_source gets manage_source wherever it had
    # manage_rules; a role without create_source, which couldn't change
    # sources before, gets nothing new. Going down drops it again.
    import json

    db = tmp_path / "perchtail.db"
    _alembic(tmp_path, "upgrade", "e3a9c5f17b28")
    roles = {
        1: ["create_source"],
        2: [],
        3: ["create_source", "manage_users"],
    }
    grants = {
        10: (1, ["view", "manage_rules"]),
        11: (1, ["view", "download"]),
        12: (2, ["view", "manage_rules"]),
        13: (3, ["manage_rules"]),
        14: (1, []),
    }
    with sqlite3.connect(db) as conn:
        for role_id, caps in roles.items():
            conn.execute(
                "INSERT INTO role (id, name, is_builtin, is_super_admin, global_capabilities) "
                "VALUES (?, ?, 0, 0, ?)",
                (role_id, f"r{role_id}", json.dumps(caps)),
            )
        for grant_id, (role_id, caps) in grants.items():
            conn.execute(
                "INSERT INTO rolegrant (id, role_id, scope_type, scope_id, capabilities) "
                "VALUES (?, ?, 'customer', ?, ?)",
                (grant_id, role_id, grant_id, json.dumps(caps)),
            )

    def capabilities():
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT id, capabilities FROM rolegrant").fetchall()
        return {grant_id: json.loads(caps) for grant_id, caps in rows}

    _alembic(tmp_path, "upgrade", "head")
    assert capabilities() == {
        10: ["view", "manage_rules", "manage_source"],
        11: ["view", "download"],
        12: ["view", "manage_rules"],
        13: ["manage_rules", "manage_source"],
        14: [],
    }

    _alembic(tmp_path, "downgrade", "e3a9c5f17b28")
    assert capabilities() == {grant_id: caps for grant_id, (_, caps) in grants.items()}


# --- c7f2a9e4d136: auditlog rebuilt with AUTOINCREMENT, minimum retention ---------

BEFORE_AUDIT_ORIGIN = "b9e4d7a2c513"
CHAIN_KEY = b"m" * 32


def _audit_engine(db: Path):
    from sqlalchemy import create_engine

    return create_engine(f"sqlite:///{db}")


def _write_old_chain(db: Path, count: int) -> None:
    """`count` entries written by a version before c7f2a9e4d136: format-1
    hashes, chained from the start."""
    from datetime import datetime, timedelta

    import sqlalchemy as sa
    from app.audit_hash_chain import compute_row_hash

    table = sa.table(
        "auditlog",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("action", sa.String),
        sa.column("target_type", sa.String),
        sa.column("target_id", sa.Integer),
        sa.column("timestamp", sa.DateTime),
        sa.column("metadata", sa.JSON),
        sa.column("prev_hash", sa.String),
        sa.column("row_hash", sa.String),
    )
    prev = None
    start = datetime(2026, 11, 1, 9, 0, 0, 123456)
    engine = _audit_engine(db)
    with engine.begin() as conn:
        for i in range(1, count + 1):
            fields = {
                "user_id": None,
                "action": "source.update",
                "target_type": "source",
                "target_id": i,
                "timestamp": start + timedelta(minutes=i),
            }
            metadata = {"n": i}
            row_hash = compute_row_hash(CHAIN_KEY, prev, metadata=metadata, **fields)
            conn.execute(
                table.insert().values(
                    id=i, prev_hash=prev, row_hash=row_hash, metadata=metadata, **fields
                )
            )
            prev = row_hash
    engine.dispose()


def _set_retention(db: Path, value: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO system_setting (key, value) VALUES ('audit_retention_days', ?)", (value,)
        )


def _retention(db: Path) -> str:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT value FROM system_setting WHERE key = 'audit_retention_days'"
        ).fetchone()[0]


def test_the_audit_chain_still_verifies_after_the_rebuild(tmp_path, monkeypatch):
    from app.audit import record_audit_event
    from app.audit_hash_chain import backfill_chain_if_needed
    from app.audit_integrity import verify_chain
    from app.audit_outbox import drain_audit_outbox
    from app.auth.models import AuditLog
    from sqlmodel import Session, select

    monkeypatch.setattr("app.audit.audit_chain_key", lambda: CHAIN_KEY)
    db = tmp_path / "perchtail.db"
    _alembic(tmp_path, "upgrade", BEFORE_AUDIT_ORIGIN)
    _write_old_chain(db, 3)
    _set_retention(db, "30")

    _alembic(tmp_path, "upgrade", "head")
    assert _autoincrement_tables(db)["auditlog"] is True
    assert _retention(db) == "90"

    # Nothing in the audit log itself: the change waits in the outbox.
    assert _audit_actions(db) == ["source.update"] * 3
    assert _outbox(db) == [
        ("system_settings.update", {"audit_retention_days": {"old": 30, "new": 90}})
    ]

    engine = _audit_engine(db)
    with Session(engine) as session:
        # What the next start does (app.main's lifespan).
        assert backfill_chain_if_needed(CHAIN_KEY, session) == 0
        assert drain_audit_outbox(session) == (1, 0)
        assert drain_audit_outbox(session) == (0, 0)
        raised = session.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
        assert raised.id == 4
        assert raised.action == "system_settings.update"
        assert raised.target_type == "system_settings"
        assert raised.user_id is None
        assert raised.event_metadata == {"audit_retention_days": {"old": 30, "new": 90}}
        assert raised.hash_format == 2
        assert raised.prev_hash == session.get(AuditLog, 3).row_hash
        assert verify_chain(CHAIN_KEY, session).ok

        record_audit_event(session, user_id=None, action="user.logout", target_type="user")
        session.commit()
        assert verify_chain(CHAIN_KEY, session).ok

        # A new-format row changed afterwards is still caught.
        newest = session.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
        assert newest.hash_format == 2
        newest.client_ip = "203.0.113.66"
        session.add(newest)
        session.commit()
        assert not verify_chain(CHAIN_KEY, session).ok
    engine.dispose()
    assert _outbox(db) == []


def _audit_actions(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT action FROM auditlog ORDER BY id")]


def _outbox(db: Path) -> list[tuple[str, dict]]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT action, metadata FROM audit_outbox ORDER BY id").fetchall()
    return [(action, json.loads(metadata)) for action, metadata in rows]


def test_a_forged_row_is_not_signed_and_the_migration_entry_lands_at_the_end(tmp_path, monkeypatch):
    from app.audit_hash_chain import backfill_chain_if_needed
    from app.audit_integrity import verify_chain
    from app.audit_outbox import drain_audit_outbox
    from app.auth.models import AuditLog
    from sqlmodel import Session, select

    monkeypatch.setattr("app.audit.audit_chain_key", lambda: CHAIN_KEY)
    db = tmp_path / "perchtail.db"
    _alembic(tmp_path, "upgrade", BEFORE_AUDIT_ORIGIN)
    _write_old_chain(db, 3)
    with sqlite3.connect(db) as conn:
        # Someone with write access to the file, before the upgrade.
        conn.execute(
            "INSERT INTO auditlog (id, action, timestamp) "
            "VALUES (4, 'forged', '2026-11-13 00:00:00')"
        )
    _set_retention(db, "30")

    _alembic(tmp_path, "upgrade", "head")

    engine = _audit_engine(db)
    with Session(engine) as session:
        assert backfill_chain_if_needed(CHAIN_KEY, session) == 0
        assert drain_audit_outbox(session) == (1, 0)
        rows = {row.id: row for row in session.exec(select(AuditLog)).all()}
        assert rows[4].row_hash is None
        assert rows[5].action == "system_settings.update"
        assert rows[5].prev_hash == rows[3].row_hash
        result = verify_chain(CHAIN_KEY, session)
        assert result.broken_row_id == 4
        assert result.reason == "unsigned_row"
    engine.dispose()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("1", "90"), ("89", "90"), ("0", "0"), ("90", "90"), ("400", "400")],
)
def test_only_a_retention_below_the_minimum_is_raised(tmp_path, stored, expected):
    db = tmp_path / "perchtail.db"
    _alembic(tmp_path, "upgrade", BEFORE_AUDIT_ORIGIN)
    _set_retention(db, stored)

    _alembic(tmp_path, "upgrade", "head")
    assert _retention(db) == expected
    assert _audit_actions(db) == []
    assert len(_outbox(db)) == (1 if stored != expected else 0)


def test_the_redaction_migration_adds_its_tables_and_clears_alert_heads(tmp_path):
    # d4a8f2c61e35 (#382): redaction_rule (with AUTOINCREMENT, so an override
    # never points at a reused id) and source_redaction_override; the stored
    # alert heads are cleared, the offsets kept.
    db = tmp_path / "perchtail.db"
    _alembic(tmp_path, "upgrade", "a8d3f5c27e14")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO source (id, name, protocol, host, base_path, enabled, is_system, "
            "check_failures, name_index_enabled, agent_upgrade_policy, agent_upgrade_failures) "
            "VALUES (1, 'a', 'ssh', 'h', '/', 1, 0, 0, 1, 'inherit', 0)"
        )
        conn.execute(
            "INSERT INTO role (id, name, is_builtin, is_super_admin, global_capabilities) "
            "VALUES (1, 'r', 0, 0, '[]')"
        )
        conn.execute(
            "INSERT INTO user (id, username, role_id, auth_provider, active, "
            "must_change_password) VALUES (1, 'u', 1, 'local', 1, 0)"
        )
        conn.execute(
            "INSERT INTO alert (id, user_id, name, query, regex, file_glob, webhook_url, "
            "enabled, failed_deliveries, created_at) "
            "VALUES (1, 1, 'x', 'q', 0, '**/*.log', 'https://example.com', 1, 0, "
            "'2026-11-14 00:00:00')"
        )
        conn.execute(
            "INSERT INTO alert_file_state (alert_id, source_id, file_path, offset, head) "
            "VALUES (1, 1, 'app.log', 1234, 'abcd')"
        )

    _alembic(tmp_path, "upgrade", "head")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT offset, head FROM alert_file_state").fetchone() == (1234, None)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'redaction_rule'"
        ).fetchone()[0]
        assert "AUTOINCREMENT" in sql.upper()
        conn.execute(
            'INSERT INTO redaction_rule (name, detector, enabled, "order") '
            "VALUES ('card', 'card', 0, 0)"
        )
        conn.execute(
            "INSERT INTO source_redaction_override (source_id, rule_id, disabled) VALUES (1, 1, 1)"
        )

    _alembic(tmp_path, "downgrade", "a8d3f5c27e14")
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"redaction_rule", "source_redaction_override"} & tables
    _alembic(tmp_path, "upgrade", "head")


def test_audit_ids_are_never_reused_after_the_upgrade(tmp_path):
    # A purge before the upgrade left its anchor at row 50 and every row
    # after it went too: the next entry must still get an id above 50.
    db = tmp_path / "perchtail.db"
    _alembic(tmp_path, "upgrade", BEFORE_AUDIT_ORIGIN)
    _write_old_chain(db, 3)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO audit_chain_state (id, anchor_row_id, anchor_hash, status) "
            "VALUES (1, 50, 'h', 'unknown')"
        )

    _alembic(tmp_path, "upgrade", "head")
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM auditlog")
        conn.execute(
            "INSERT INTO auditlog (action, timestamp) "
            "VALUES ('user.logout', '2026-11-14 00:00:00')"
        )
        [new_id] = conn.execute("SELECT id FROM auditlog").fetchone()
    assert new_id == 51


# --- a8d3f5c27e14: one grant per role and scope -----------------------------------

BEFORE_ONE_GRANT_PER_SCOPE = "d4a8f2c6b913"


def _grants(db: Path) -> dict[int, tuple]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT id, role_id, scope_type, scope_id, capabilities FROM rolegrant"
        ).fetchall()
    return {row[0]: (row[1], row[2], row[3], json.loads(row[4])) for row in rows}


def _insert_grant(conn, grant_id, role_id, scope_type, scope_id, caps) -> None:
    conn.execute(
        "INSERT INTO rolegrant (id, role_id, scope_type, scope_id, capabilities) "
        "VALUES (?, ?, ?, ?, ?)",
        (grant_id, role_id, scope_type, scope_id, json.dumps(caps)),
    )


def test_duplicate_grants_are_merged_into_the_one_resolution_used(tmp_path, monkeypatch):
    from app.audit_hash_chain import backfill_chain_if_needed
    from app.audit_integrity import verify_chain
    from app.audit_outbox import drain_audit_outbox
    from app.auth.models import AuditLog
    from sqlmodel import Session, select

    monkeypatch.setattr("app.audit.audit_chain_key", lambda: CHAIN_KEY)
    db = tmp_path / "perchtail.db"
    _alembic(tmp_path, "upgrade", BEFORE_ONE_GRANT_PER_SCOPE)
    with sqlite3.connect(db) as conn:
        for role_id in (1, 2):
            conn.execute(
                "INSERT INTO role (id, name, is_builtin, is_super_admin, global_capabilities) "
                "VALUES (?, ?, 0, 0, '[]')",
                (role_id, f"r{role_id}"),
            )
        _insert_grant(conn, 10, 1, "customer", 5, ["view"])
        _insert_grant(conn, 11, 1, "customer", 5, ["view", "download"])
        _insert_grant(conn, 12, 1, "folder", 5, ["view"])
        _insert_grant(conn, 13, 2, "customer", 5, ["view", "manage_rules"])
        _insert_grant(conn, 14, 1, "customer", 5, [])
        _insert_grant(conn, 15, 2, "source", 7, ["view"])

    _alembic(tmp_path, "upgrade", "head")

    # The lowest id of each role and scope stays; the others are gone.
    assert _grants(db) == {
        10: (1, "customer", 5, ["view"]),
        12: (1, "folder", 5, ["view"]),
        13: (2, "customer", 5, ["view", "manage_rules"]),
        15: (2, "source", 7, ["view"]),
    }
    assert _audit_actions(db) == []
    assert _outbox(db) == [
        (
            "role_grant.delete",
            {
                "reason": "duplicate_scope",
                "grant_id": 11,
                "kept_grant_id": 10,
                "role_id": 1,
                "scope_type": "customer",
                "scope_id": 5,
                "capabilities": ["view", "download"],
            },
        ),
        (
            "role_grant.delete",
            {
                "reason": "duplicate_scope",
                "grant_id": 14,
                "kept_grant_id": 10,
                "role_id": 1,
                "scope_type": "customer",
                "scope_id": 5,
                "capabilities": [],
            },
        ),
    ]

    # The constraint refuses a new duplicate.
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        _insert_grant(conn, 20, 1, "customer", 5, ["view"])

    # The next start records each deletion as a signed entry with no user.
    engine = _audit_engine(db)
    with Session(engine) as session:
        assert backfill_chain_if_needed(CHAIN_KEY, session) == 0
        assert drain_audit_outbox(session) == (2, 0)
        entries = session.exec(select(AuditLog).order_by(AuditLog.id)).all()
        assert [(e.action, e.target_type, e.target_id, e.user_id) for e in entries] == [
            ("role_grant.delete", "role_grant", 11, None),
            ("role_grant.delete", "role_grant", 14, None),
        ]
        assert entries[0].event_metadata["capabilities"] == ["view", "download"]
        assert verify_chain(CHAIN_KEY, session).ok
    engine.dispose()

    # Down drops the constraint (the deleted grants stay deleted); up again
    # brings it back.
    _alembic(tmp_path, "downgrade", BEFORE_ONE_GRANT_PER_SCOPE)
    with sqlite3.connect(db) as conn:
        _insert_grant(conn, 20, 1, "customer", 5, ["view"])
    assert set(_grants(db)) == {10, 12, 13, 15, 20}

    _alembic(tmp_path, "upgrade", "head")
    assert set(_grants(db)) == {10, 12, 13, 15}
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        _insert_grant(conn, 21, 1, "customer", 5, ["view"])
