"""Every migration that rebuilds a table must keep AUTOINCREMENT on the
tables whose ids must never be reused (see app.models.NO_ID_REUSE and
migration 47c9d392f28f): a rebuild through Alembic's batch mode drops it
unless it's asked for, as e4b7c2d91a05 once did for `source`."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
NO_ID_REUSE_TABLES = ("customer", "folder", "source", "role")


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
