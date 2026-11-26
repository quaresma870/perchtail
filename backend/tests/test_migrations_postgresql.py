"""Migrations on both engines (#306): the revisions before pg_baseline only
run on SQLite (migrations/engines.py), pg_baseline creates the same schema
on an empty PostgreSQL database, `alembic check` finds nothing on either,
and the last revision goes down and up on both."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from app import instance_lock
from app.name_index_store import PG_TRGM_INDEX
from sqlalchemy import create_engine, inspect, text

BACKEND = Path(__file__).resolve().parents[1]
VERSIONS = BACKEND / "migrations" / "versions"
BASELINE = "c4e7a1d9b302"
BEFORE_BASELINE = "a7c3e9d2f514"
# What only one engine has: SQLite's FTS5 tables and its sequence table,
# PostgreSQL's trigram index.
_ENGINE_ONLY_TABLES = re.compile(r"^(file_name_fts.*|sqlite_sequence|alembic_version)$")


def _alembic(tmp_path: Path, url: str | None, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATA_DIR": str(tmp_path / "db"), "LOG_DIR": str(tmp_path / "logs")}
    env.pop("DATABASE_URL", None)
    env.pop("TEST_DATABASE_URL", None)
    if url:
        env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'db' / 'perchtail.db'}"


def _version(url: str) -> str:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def test_every_revision_before_the_baseline_runs_on_sqlite_only():
    revisions = [path for path in VERSIONS.glob("*.py") if BASELINE not in path.name]
    assert len(revisions) == 47
    for path in revisions:
        source = path.read_text(encoding="utf-8")
        assert "from migrations.engines import sqlite_only" in source, path.name
        for function in ("upgrade", "downgrade"):
            body = source.split(f"def {function}() -> None:", 1)[1]
            # The guard comes first, after an optional docstring or comments.
            body = re.sub(r'^\s*""".*?"""', "", body, count=1, flags=re.S)
            code = [
                line.strip()
                for line in body.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            assert code[:2] == ["if not sqlite_only():", "return"], (path.name, function)


def test_the_baseline_is_the_only_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini")))
    assert script.get_heads() == [BASELINE]
    assert script.get_revision(BASELINE).down_revision == BEFORE_BASELINE


def test_sqlite_upgrades_to_the_baseline_without_a_change_and_checks_clean(tmp_path):
    _ok(_alembic(tmp_path, None, "upgrade", BEFORE_BASELINE))
    url = _sqlite_url(tmp_path)
    before = _schema(url)
    _ok(_alembic(tmp_path, None, "upgrade", "head"))
    assert _version(url) == BASELINE
    assert _schema(url) == before
    check = _ok(_alembic(tmp_path, None, "check"))
    assert "No new upgrade operations detected" in check.stdout + check.stderr


@pytest.mark.postgres_only
def test_an_empty_postgresql_database_upgrades_to_head_and_checks_clean(
    tmp_path, empty_postgres_url
):
    _ok(_alembic(tmp_path, empty_postgres_url, "upgrade", "head"))
    assert _version(empty_postgres_url) == BASELINE
    engine = create_engine(empty_postgres_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id FROM update_check_state")).scalar() == 1
            index = connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": PG_TRGM_INDEX}
            ).scalar()
            enums = connection.execute(text("SELECT count(*) FROM pg_type WHERE typtype = 'e'"))
            [(enum_types,)] = enums.all()
            columns = {
                (row.table_name, row.column_name): row.data_type
                for row in connection.execute(
                    text(
                        "SELECT table_name, column_name, data_type "
                        "FROM information_schema.columns WHERE table_schema = 'public'"
                    )
                )
            }
    finally:
        engine.dispose()
    assert "gin_trgm_ops" in index
    assert enum_types == 0
    assert columns[("file_name_entry", "size")] == "bigint"
    assert columns[("alert_file_state", "offset")] == "bigint"
    assert columns[("source", "protocol")] == "character varying"
    assert columns[("rolegrant", "scope_type")] == "character varying"
    check = _ok(_alembic(tmp_path, empty_postgres_url, "check"))
    assert "No new upgrade operations detected" in check.stdout + check.stderr


def _schema(url: str) -> dict:
    """Tables, their columns (name and nullability), indexes, unique
    constraints and foreign keys, as the engine reports them."""
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        schema = {}
        for table in sorted(inspector.get_table_names()):
            if _ENGINE_ONLY_TABLES.match(table):
                continue
            columns = {c["name"]: c["nullable"] for c in inspector.get_columns(table)}
            primary_key = tuple(inspector.get_pk_constraint(table)["constrained_columns"])
            indexes = {
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table)
                if index["name"] != PG_TRGM_INDEX and not index.get("duplicates_constraint")
            }
            uniques = {
                (unique["name"], tuple(unique["column_names"]))
                for unique in inspector.get_unique_constraints(table)
            }
            foreign_keys = {
                (tuple(key["constrained_columns"]), key["referred_table"])
                + (tuple(key["referred_columns"]),)
                for key in inspector.get_foreign_keys(table)
            }
            schema[table] = {
                "columns": columns,
                "primary_key": primary_key,
                "indexes": indexes,
                "uniques": uniques,
                "foreign_keys": foreign_keys,
            }
        return schema
    finally:
        engine.dispose()


@pytest.mark.postgres_only
def test_the_migrated_postgresql_schema_is_the_migrated_sqlite_schema(tmp_path, empty_postgres_url):
    _ok(_alembic(tmp_path / "sqlite", None, "upgrade", "head"))
    _ok(_alembic(tmp_path / "pg", empty_postgres_url, "upgrade", "head"))

    sqlite = _schema(_sqlite_url(tmp_path / "sqlite"))
    postgres = _schema(empty_postgres_url)

    assert sorted(postgres) == sorted(sqlite)
    for table in sqlite:
        assert postgres[table] == sqlite[table], table


def _engine_url(engine: str, tmp_path: Path, request) -> str | None:
    if engine == "sqlite":
        return None
    return request.getfixturevalue("empty_postgres_url")


@pytest.mark.parametrize(
    "engine", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres_only)]
)
def test_the_last_revision_goes_down_and_up(tmp_path, request, engine):
    url = _engine_url(engine, tmp_path, request)
    database = url or _sqlite_url(tmp_path)
    _ok(_alembic(tmp_path, url, "upgrade", "head"))
    tables = set(_schema(database))

    _ok(_alembic(tmp_path, url, "downgrade", "-1"))
    assert _version(database) == BEFORE_BASELINE
    if engine == "postgresql":
        assert set(_schema(database)) == set()
    else:
        assert set(_schema(database)) == tables

    _ok(_alembic(tmp_path, url, "upgrade", "head"))
    assert _version(database) == BASELINE
    assert set(_schema(database)) == tables


@pytest.mark.postgres_only
def test_migrating_is_refused_while_another_instance_holds_the_database(
    tmp_path, empty_postgres_url
):
    engine = create_engine(empty_postgres_url)
    lock = instance_lock.acquire_database(engine)
    try:
        result = _alembic(tmp_path, empty_postgres_url, "upgrade", "head")
    finally:
        lock.release()
        engine.dispose()
    assert result.returncode != 0
    assert "startup.instance_lock_held" in result.stdout
    assert _schema(empty_postgres_url) == {}
