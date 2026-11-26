"""python -m app.migrate_database (#306): a populated SQLite database moved
into an empty PostgreSQL one in one transaction, checked before the commit
and audited; every refusal writes nothing, and a copy that doesn't check
out is rolled back."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app import db_copy, instance_lock, migrate_database
from app.audit_integrity import verify_chain
from app.auth.models import AuditLog
from app.config import get_settings
from app.crypto import audit_chain_key, decrypt_credential, encrypt_secret
from app.db import configure_sqlite_connection
from app.migrate_database import (
    MigrationRefusedError,
    VerificationFailedError,
    _upgrade_to_head,
    migrate,
)
from app.models import Customer, Source, UpdateCheckState
from app.name_index_store import PG_TRGM_INDEX, ensure_name_index_schema
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, select

from tests.migration_seed import CREDENTIAL, populate

BACKEND = Path(__file__).resolve().parents[1]
HEAD = migrate_database._head_revision()
STRONG_TEST_KEY = "q7Vt2pXk9LmN4rYs8BwE3cHj6FdA1gZu5KoTiPxQ0eR"
# Where a refusal is expected before anything connects to it.
UNREACHED_POSTGRESQL = "postgresql+psycopg://perchtail:unused@127.0.0.1:1/perchtail"


def _source(tmp_path: Path, revision: str = HEAD):
    """A SQLite database at `revision`, in WAL mode like the app's."""
    path = tmp_path / "perchtail.db"
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    event.listen(engine, "connect", configure_sqlite_connection)
    SQLModel.metadata.create_all(engine)
    ensure_name_index_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        connection.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": revision})
    return path, engine


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


@pytest.fixture()
def settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", UNREACHED_POSTGRESQL)
    encrypt_secret("the credential salt exists")  # as on any installation
    return settings


@pytest.fixture()
def target(settings, monkeypatch, empty_postgres_url):
    monkeypatch.setattr(settings, "database_url", empty_postgres_url)
    engine = create_engine(empty_postgres_url, poolclass=NullPool)
    yield engine
    engine.dispose()


def _tables(engine) -> list[str]:
    return inspect(engine).get_table_names()


def _digest(path: Path) -> dict[str, str]:
    """The database file and its WAL, where its data is; a reader may add an
    empty WAL and the shared-memory index (-shm), which hold none."""
    return {
        file.name: hashlib.sha256(file.read_bytes()).hexdigest()
        for file in (path, path.with_name(path.name + "-wal"))
        if file.exists() and file.stat().st_size
    }


@pytest.mark.postgres_only
def test_everything_is_copied_checked_and_audited(tmp_path, settings, target):
    path, source_engine = _source(tmp_path)
    with Session(source_engine) as session:
        populate(session, source_engine)
    with source_engine.connect() as connection:
        expected = db_copy.table_counts(connection)
    # The source is still open, its last writes still in the WAL file.
    held = source_engine.connect()
    before = _digest(path)
    try:
        report = migrate(_url(path), dry_run=False, settings=settings)
        assert _digest(path) == before
    finally:
        held.close()
        source_engine.dispose()

    assert report.tables == expected
    assert report.credentials_checked == 5
    assert report.audit_rows_verified == expected["auditlog"]
    with target.connect() as connection:
        counts = db_copy.table_counts(connection)
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == HEAD
        assert connection.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :n"), {"n": PG_TRGM_INDEX}
        ).scalar()
    assert counts == {**expected, "auditlog": expected["auditlog"] + 1}
    with Session(target) as session:
        entry = session.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
        assert entry.action == "database.migrate"
        assert entry.user_id is None
        assert entry.event_metadata == {
            "from_engine": "sqlite",
            "to_engine": "postgresql",
            "tables": expected,
            "rows": sum(expected.values()),
            "credentials_checked": 5,
            "audit_rows_verified": expected["auditlog"],
            "result": "ok",
        }
        assert verify_chain(audit_chain_key(), session).ok
        source = session.exec(select(Source).where(Source.credential_ref.is_not(None))).one()
        assert decrypt_credential(source.credential_ref) == CREDENTIAL
        # New rows get new ids, past the copied ones.
        highest = session.exec(select(Customer.id).order_by(Customer.id.desc())).first()
        customer = Customer(name="after the migration")
        session.add(customer)
        session.commit()
        assert customer.id == highest + 1


@pytest.mark.postgres_only
def test_a_dry_run_leaves_the_target_as_it_was(tmp_path, settings, target):
    path, source_engine = _source(tmp_path)
    with Session(source_engine) as session:
        populate(session, source_engine)
    source_engine.dispose()

    report = migrate(_url(path), dry_run=True, settings=settings)

    assert report.dry_run
    assert report.tables["file_name_entry"] > 0
    assert _tables(target) == []


@pytest.mark.postgres_only
def test_a_target_migrated_beforehand_but_empty_is_accepted(tmp_path, settings, target):
    with target.begin() as connection:
        _upgrade_to_head(connection)
    path, source_engine = _source(tmp_path)
    with Session(source_engine) as session:
        session.add(Customer(name="c"))
        session.add(UpdateCheckState(id=1, latest_version="0.7.0"))
        session.commit()
    source_engine.dispose()

    migrate(_url(path), dry_run=False, settings=settings)

    with Session(target) as session:
        [state] = session.exec(select(UpdateCheckState)).all()
        assert state.latest_version == "0.7.0"
        assert session.exec(select(Customer.name)).all() == ["c"]


@pytest.mark.postgres_only
def test_a_target_with_rows_is_refused(tmp_path, settings, target):
    with target.begin() as connection:
        _upgrade_to_head(connection)
        connection.execute(text("INSERT INTO customer (name) VALUES ('already here')"))
    path, source_engine = _source(tmp_path)
    source_engine.dispose()

    with pytest.raises(MigrationRefusedError, match=r"customer \(1\)") as refused:
        migrate(_url(path), dry_run=False, settings=settings)

    assert refused.value.reason == "target_not_empty"
    with target.connect() as connection:
        counts = db_copy.table_counts(connection)
    assert counts["customer"] == 1
    assert sum(counts.values()) == 2  # and the update_check_state row the migration made


@pytest.mark.postgres_only
def test_a_target_with_a_changed_seeded_row_is_refused(tmp_path, settings, target):
    with target.begin() as connection:
        _upgrade_to_head(connection)
        connection.execute(text("UPDATE update_check_state SET latest_version = '9.9.9'"))
    path, source_engine = _source(tmp_path)
    source_engine.dispose()

    with pytest.raises(MigrationRefusedError, match=r"update_check_state \(1\)"):
        migrate(_url(path), dry_run=False, settings=settings)


def test_refused_while_perchtail_runs_on_the_data_dir(tmp_path, settings):
    path, source_engine = _source(tmp_path)
    source_engine.dispose()
    lock = instance_lock.acquire(settings)
    try:
        with pytest.raises(MigrationRefusedError) as refused:
            migrate(_url(path), dry_run=False, settings=settings)
    finally:
        lock.release()
    assert refused.value.reason == "instance_running"


@pytest.mark.postgres_only
def test_refused_while_perchtail_runs_on_the_database(tmp_path, settings, target):
    path, source_engine = _source(tmp_path)
    source_engine.dispose()
    lock = instance_lock.acquire_database(target)
    try:
        with pytest.raises(MigrationRefusedError) as refused:
            migrate(_url(path), dry_run=False, settings=settings)
    finally:
        lock.release()
    assert refused.value.reason == "instance_running"
    assert _tables(target) == []


def test_a_source_at_another_revision_is_refused(tmp_path, settings):
    path, source_engine = _source(tmp_path, revision="a7c3e9d2f514")
    source_engine.dispose()
    before = _digest(path)

    with pytest.raises(MigrationRefusedError, match="start this version") as refused:
        migrate(_url(path), dry_run=False, settings=settings)

    assert refused.value.reason == "source_revision"
    assert _digest(path) == before


@pytest.mark.parametrize(
    "url",
    [UNREACHED_POSTGRESQL, "sqlite://", "sqlite:///:memory:", "mysql://u@h/d"],
)
def test_a_source_that_isnt_a_sqlite_file_is_refused(settings, url):
    with pytest.raises(MigrationRefusedError) as refused:
        migrate(url, dry_run=False, settings=settings)
    assert refused.value.reason == "source_not_sqlite"


def test_a_missing_source_file_is_refused(tmp_path, settings):
    with pytest.raises(MigrationRefusedError) as refused:
        migrate(_url(tmp_path / "missing.db"), dry_run=False, settings=settings)
    assert refused.value.reason == "source_missing"
    assert not (tmp_path / "missing.db").exists()


def test_a_target_that_isnt_postgresql_is_refused(tmp_path, settings, monkeypatch):
    path, source_engine = _source(tmp_path)
    source_engine.dispose()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'other.db'}")

    with pytest.raises(MigrationRefusedError) as refused:
        migrate(_url(path), dry_run=False, settings=settings)

    assert refused.value.reason == "target_not_postgresql"
    assert not (tmp_path / "other.db").exists()


def test_a_missing_credential_salt_is_refused(tmp_path, settings, monkeypatch):
    path, source_engine = _source(tmp_path)
    source_engine.dispose()
    monkeypatch.setattr(settings, "credential_salt_path", str(tmp_path / "no-salt"))

    with pytest.raises(MigrationRefusedError) as refused:
        migrate(_url(path), dry_run=False, settings=settings)

    assert refused.value.reason == "credential_salt_missing"
    assert not (tmp_path / "no-salt").exists()


@pytest.mark.postgres_only
def test_secrets_another_key_encrypted_roll_the_copy_back(tmp_path, settings, target, monkeypatch):
    path, source_engine = _source(tmp_path)
    with Session(source_engine) as session:
        populate(session, source_engine)
    source_engine.dispose()
    monkeypatch.setattr(settings, "credential_encryption_key", "not the key that encrypted them")

    with pytest.raises(VerificationFailedError) as failed:
        migrate(_url(path), dry_run=False, settings=settings)

    problems = " ".join(failed.value.problems)
    assert "doesn't decrypt: source id=" in problems
    assert "SMTP password" in problems and "webhook URL" in problems
    assert "SSO provider" in problems and "MFA secret" in problems
    assert "audit chain doesn't verify" in problems
    assert _tables(target) == []


@pytest.mark.postgres_only
def test_a_tampered_audit_chain_rolls_the_copy_back(tmp_path, settings, target):
    path, source_engine = _source(tmp_path)
    with Session(source_engine) as session:
        populate(session, source_engine)
    with source_engine.begin() as connection:
        connection.execute(
            text("UPDATE auditlog SET action = 'forged' WHERE id = (SELECT max(id) FROM auditlog)")
        )
    source_engine.dispose()

    with pytest.raises(VerificationFailedError) as failed:
        migrate(_url(path), dry_run=False, settings=settings)

    [problem] = failed.value.problems
    assert "audit chain doesn't verify" in problem
    assert _tables(target) == []


def _stored_secrets(engine) -> list[str]:
    """Every encrypted secret and password hash in the database."""
    with engine.connect() as connection:
        values = connection.execute(
            text(
                'SELECT credential_ref FROM source UNION ALL SELECT password_hash FROM "user" '
                'UNION ALL SELECT mfa_secret_encrypted FROM "user" '
                "UNION ALL SELECT token_hash FROM authsession"
            )
        ).scalars()
        return [value for value in values if value]


def _fails_without_values(tmp_path, settings, target, capsys, *, table: str, reason: str):
    """Runs the command on the source at tmp_path; it must fail naming the
    table and the reason, with none of the rows' values on stderr or in the
    log, and leave the target empty."""
    from structlog.testing import capture_logs

    engine = create_engine(_url(tmp_path / "perchtail.db"), poolclass=NullPool)
    secrets = _stored_secrets(engine)
    engine.dispose()
    assert len(secrets) >= 3

    with capture_logs() as logs:
        code = migrate_database.main(["--from", _url(tmp_path / "perchtail.db")])

    err = capsys.readouterr().err
    assert code == 1
    assert f"a row of {table} doesn't fit PostgreSQL: {reason}" in err
    assert "rolled back" in err
    logged = json.dumps(logs, default=str)
    assert '"result": "failed"' in logged and f'"table": "{table}"' in logged
    for secret in secrets:
        assert secret not in err
        assert secret not in logged
    assert "parameters" not in err
    assert _tables(target) == []


@pytest.mark.postgres_only
def test_an_orphaned_row_fails_without_printing_its_values(
    tmp_path, settings, target, monkeypatch, capsys
):
    monkeypatch.setattr(migrate_database, "configure_logging", lambda: None)
    path, source_engine = _source(tmp_path)
    with Session(source_engine) as session:
        populate(session, source_engine)
    # SQLite doesn't enforce foreign keys: a user whose role is gone.
    with source_engine.begin() as connection:
        connection.execute(text('UPDATE "user" SET role_id = 9999'))
    source_engine.dispose()

    _fails_without_values(
        tmp_path,
        settings,
        target,
        capsys,
        table="user",
        reason='insert or update on table "user" violates foreign key constraint',
    )


@pytest.mark.postgres_only
def test_a_value_too_long_fails_without_printing_its_values(
    tmp_path, settings, target, monkeypatch, capsys
):
    # SQLite doesn't enforce VARCHAR lengths, PostgreSQL does. No column of
    # the models has a length a valid row can pass, so the target's is made
    # shorter than the source's names.
    migrate_to_head = migrate_database._upgrade_to_head

    def narrowed(connection):
        migrate_to_head(connection)
        connection.execute(text("ALTER TABLE source ALTER COLUMN name TYPE VARCHAR(2)"))

    monkeypatch.setattr(migrate_database, "_upgrade_to_head", narrowed)
    monkeypatch.setattr(migrate_database, "configure_logging", lambda: None)
    path, source_engine = _source(tmp_path)
    with Session(source_engine) as session:
        populate(session, source_engine)
    source_engine.dispose()

    _fails_without_values(
        tmp_path,
        settings,
        target,
        capsys,
        table="source",
        reason="value too long for type character varying(2)",
    )


def test_the_command_exits_non_zero_on_a_refusal(tmp_path, settings, monkeypatch, capsys):
    monkeypatch.setattr(migrate_database, "configure_logging", lambda: None)

    code = migrate_database.main(["--from", _url(tmp_path / "missing.db")])

    assert code == 1
    assert "nothing was written" in capsys.readouterr().err


# The whole way, as an operator runs it: a SQLite installation filled by the
# app, the command, then the app started on PostgreSQL.


def _environment(tmp_path: Path, url: str | None) -> dict[str, str]:
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path / "db"),
        "LOG_DIR": str(tmp_path / "logs"),
        "SCRATCH_DIR": str(tmp_path / "scratch"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "CREDENTIAL_ENCRYPTION_KEY": STRONG_TEST_KEY,
        "UPDATE_CHECK": "false",
        "SESSION_COOKIE_SECURE": "false",
    }
    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "CREDENTIAL_SALT_PATH"):
        env.pop(name, None)
    if url:
        env["DATABASE_URL"] = url
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, result.stdout + result.stderr
    return result


_SEED = """
from fastapi.testclient import TestClient
from sqlmodel import Session
from app import db as app_db
from app.main import app as fastapi_app
from tests.migration_seed import populate

with TestClient(fastapi_app, client=("127.0.0.1", 50000)):
    with Session(app_db.engine) as session:
        populate(session, app_db.engine)
"""

_PROBE = """
import json
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app import db as app_db
from app.audit_integrity import verify_chain
from app.auth.models import AuditLog, AuditOutbox
from app.crypto import audit_chain_key, decrypt_credential
from app.main import app as fastapi_app
from app.models import Source
from tests.migration_seed import READER, READER_PASSWORD

out = {}
with TestClient(fastapi_app, client=("127.0.0.1", 50000)) as client:
    out["healthz"] = client.get("/healthz").status_code
    login = client.post("/auth/login", json={"username": READER, "password": READER_PASSWORD})
    out["login"] = login.status_code
    out["sources"] = client.get("/sources").json()
    out["search"] = {
        q: client.get("/files", params={"q": q, "limit": 1000}).json() for q in ("", "ap", "12")
    }
    with Session(app_db.engine) as session:
        source = session.exec(select(Source).where(Source.name == "migrated-app01")).one()
        out["credential"] = decrypt_credential(source.credential_ref)
        chain = verify_chain(audit_chain_key(), session)
        out["chain"] = [chain.ok, chain.reason]
        out["actions"] = list(session.exec(select(AuditLog.action).order_by(AuditLog.id)).all())
        out["outbox"] = len(session.exec(select(AuditOutbox)).all())
        out["engine"] = app_db.engine.dialect.name
print("PROBE" + json.dumps(out, default=str))
"""


def _probe(env: dict[str, str]) -> dict:
    result = _ok(_run(env, "-c", _PROBE))
    [line] = [line for line in result.stdout.splitlines() if line.startswith("PROBE")]
    return json.loads(line.removeprefix("PROBE"))


def _logged(tmp_path: Path, *results: subprocess.CompletedProcess) -> str:
    logs = "".join(
        path.read_text(errors="replace") for path in (tmp_path / "logs").glob("*") if path.is_file()
    )
    return logs + "".join(result.stdout + result.stderr for result in results)


@pytest.mark.postgres_only
def test_a_populated_sqlite_installation_runs_on_postgresql_after_the_command(
    tmp_path, empty_postgres_url
):
    sqlite = _environment(tmp_path, None)
    database = tmp_path / "db" / "perchtail.db"
    _ok(_run(sqlite, "-m", "alembic", "upgrade", "head"))
    _ok(_run(sqlite, "-c", _SEED))
    before = _probe(sqlite)
    assert before["engine"] == "sqlite"
    assert before["login"] == 200
    assert before["chain"] == [True, None]
    assert before["search"][""]["hits"]

    postgresql = _environment(tmp_path, empty_postgres_url)
    migrated = _ok(
        _run(postgresql, "-m", "app.migrate_database", "--from", f"sqlite:///{database}")
    )
    assert "Migrated" in migrated.stdout
    after = _probe(postgresql)

    assert after["engine"] == "postgresql"
    assert after["healthz"] == 200
    assert after["login"] == 200
    assert after["credential"] == before["credential"] == CREDENTIAL
    assert after["chain"] == [True, None]
    assert after["outbox"] == before["outbox"] == 0
    assert after["sources"] == before["sources"]
    assert after["search"] == before["search"]
    copied = len(before["actions"])
    assert after["actions"][:copied] == before["actions"]
    assert "database.migrate" in after["actions"][copied:]

    password = make_url(empty_postgres_url).password
    everything = _logged(tmp_path, migrated)
    assert '"event": "database.migrate"' in everything
    assert '"result": "ok"' in everything
    # The URL is shown without its password (the test role's password may
    # be its user name, which is shown).
    assert "***@" in everything
    for secret in (f":{password}@", "ssh-s3cret", "smtp-s3cret", "oidc-s3cret", "hooks.example"):
        assert secret not in everything
