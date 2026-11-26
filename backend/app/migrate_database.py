"""Moves a PerchTail installation from SQLite to PostgreSQL (#306).

Usage (from backend/, with PerchTail stopped, DATABASE_URL pointing at the
new PostgreSQL database, and the installation's own DATA_DIR,
CREDENTIAL_ENCRYPTION_KEY and credential salt):

    DATABASE_URL=postgresql://... python -m app.migrate_database \\
        --from sqlite:////data/db/perchtail.db [--dry-run]

It refuses, writing nothing, when PerchTail runs on this DATA_DIR or on the
target database, when --from isn't a SQLite database file or isn't at this
version's revision (start this version on it once first), when DATABASE_URL
isn't PostgreSQL or the server is one PerchTail wouldn't start on, when the
credential salt is missing, and when any of PerchTail's tables in the target
holds a row (create the database again; there is no way to force it).

Then, in one transaction on the target: an empty database is migrated to
this version's schema, every table is copied with its ids (app/db_copy.py),
the id sequences are moved past them, and the result is checked: the same
number of rows in every table, every stored secret (source credentials, SSO
provider settings, MFA secrets, the SMTP password, webhook URLs) decrypts
with the current key, the audit chain verifies, and the revision is the
source's. Only then is the migration recorded in the audit log
(`database.migrate`, chained after the copied entries), the chain verified
once more and the transaction committed. Anything that fails rolls it all
back: the target is left as it was. The SQLite file is opened read-only and
never changed, so going back is pointing DATABASE_URL at it again.
`--dry-run` does everything, then rolls back.

Secrets are copied as they are stored, encrypted; they are decrypted only to
check them, and neither they nor the database password are ever printed or
logged."""

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from cryptography.fernet import InvalidToken
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel
from sqlmodel import select as sqlmodel_select

from app import db_copy, instance_lock
from app.audit_hash_chain import chain_new_entry
from app.audit_integrity import verify_chain
from app.auth.models import AuditLog, SSOProviderConfig, User
from app.backup import ROOT_REFUSAL
from app.config import Settings, get_settings, is_postgresql_url, redacted_database_url
from app.crypto import build_audit_chain_key, build_fernet
from app.db import DatabaseServerRefusedError, check_database_server, engine_options
from app.logging_config import configure_logging, get_logger
from app.models import Alert, SmtpSettings, Source, UpdateCheckState
from app.schema_version import MIGRATIONS_DIR
from app.timeutils import utcnow

logger = get_logger(__name__)

AUDIT_ACTION = "database.migrate"

# Tables the migrations themselves fill on an empty PostgreSQL database
# (pg_baseline inserts update_check_state's singleton row): a target whose
# only rows are these, untouched, still counts as empty, and they are
# replaced by the source's.
_SEEDED_BY_MIGRATIONS = {UpdateCheckState.__tablename__: {"id": 1}}


class MigrationRefusedError(Exception):
    """A precondition failed; nothing was written. `reason` is a short code
    for the log."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


class VerificationFailedError(Exception):
    """The copy didn't check out; the target's transaction was rolled back."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class MigrationReport:
    tables: dict[str, int]
    credentials_checked: int
    audit_rows_verified: int
    dry_run: bool
    revision: str
    problems: list[str] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return sum(self.tables.values())


def _head_revision() -> str:
    return ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()


def _sqlite_file(url: str) -> Path:
    """The SQLite database file --from names, which must exist."""
    if not url.startswith("sqlite:///"):
        raise MigrationRefusedError(
            "source_not_sqlite",
            "--from must name the SQLite database file, as sqlite:////data/db/perchtail.db",
        )
    path = url.removeprefix("sqlite:///")
    if not path or path == ":memory:" or "?" in path:
        raise MigrationRefusedError(
            "source_not_sqlite",
            "--from must name the SQLite database file, as sqlite:////data/db/perchtail.db",
        )
    file = Path(path)
    if not file.is_file():
        raise MigrationRefusedError(
            "source_missing", f"the SQLite database {file} doesn't exist or isn't a file"
        )
    return file


def _read_only_engine(file: Path) -> Engine:
    """The SQLite file, opened read-only (mode=ro): nothing can be written
    to it through this engine."""
    uri = file.resolve().as_uri() + "?mode=ro"

    def connect():
        return sqlite3.connect(uri, uri=True, check_same_thread=False)

    # hide_parameters: an error's message never carries a row's values.
    return create_engine("sqlite://", creator=connect, poolclass=NullPool, hide_parameters=True)


def _target_engine(url: str) -> Engine:
    connect_args = engine_options(url).get("connect_args", {})
    return create_engine(url, poolclass=NullPool, connect_args=connect_args, hide_parameters=True)


def _revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _occupied_tables(connection: Connection) -> list[str]:
    """PerchTail's tables in the target that hold a row, other than a row
    the migrations put there themselves."""
    occupied = []
    tables = db_copy.existing_tables(connection)
    counts = db_copy.table_counts(connection, tables)
    for table in tables:
        count = counts[table.name]
        if count == 0:
            continue
        seed = _SEEDED_BY_MIGRATIONS.get(table.name)
        if seed is not None and count == 1:
            row = connection.execute(select(table)).mappings().one()
            if all(row[name] == seed.get(name) for name in row.keys()):
                continue
        occupied.append(f"{table.name} ({count})")
    return occupied


def _clear_migration_seeds(connection: Connection) -> None:
    for name in _SEEDED_BY_MIGRATIONS:
        table = SQLModel.metadata.tables[name]
        connection.execute(table.delete())


def _upgrade_to_head(connection: Connection) -> None:
    """This version's migrations, on the target's connection and in its
    open transaction (PostgreSQL's DDL is transactional), without
    migrations/env.py, whose locks this process already holds."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(config)

    def upgrade(revision, _context):
        return script._upgrade_revs("head", revision)

    with EnvironmentContext(config, script, fn=upgrade, destination_rev="head") as environment:
        environment.configure(connection=connection, target_metadata=SQLModel.metadata)
        with environment.begin_transaction():
            environment.run_migrations()


def _undecryptable(session: Session, secret: str) -> tuple[int, list[str]]:
    """How many stored secrets were checked, and the ones the current key
    doesn't decrypt. The plain values are dropped at once."""
    fernet = build_fernet(secret)
    checked = 0
    failed: list[str] = []
    stored = [
        (f"source id={row.id}", row.credential_ref)
        for row in session.exec(
            sqlmodel_select(Source).where(Source.credential_ref.is_not(None))
        ).all()
    ]
    stored += [
        (f"SSO provider id={row.id}", row.config)
        for row in session.exec(sqlmodel_select(SSOProviderConfig)).all()
    ]
    stored += [
        (f"MFA secret of user id={row.id}", row.mfa_secret_encrypted)
        for row in session.exec(
            sqlmodel_select(User).where(User.mfa_secret_encrypted.is_not(None))
        ).all()
    ]
    stored += [
        ("SMTP password", row.password_encrypted)
        for row in session.exec(
            sqlmodel_select(SmtpSettings).where(SmtpSettings.password_encrypted.is_not(None))
        ).all()
    ]
    stored += [
        (f"webhook URL of alert id={row.id}", row.webhook_url_encrypted)
        for row in session.exec(sqlmodel_select(Alert)).all()
    ]
    for label, token in stored:
        checked += 1
        try:
            fernet.decrypt(token.encode("utf-8"))
        except (InvalidToken, ValueError):
            failed.append(label)
    return checked, failed


def _verify(
    target: Connection,
    session: Session,
    *,
    settings: Settings,
    expected: dict[str, int],
    revision: str,
) -> tuple[int, int]:
    """Checks the copy before it is committed; raises
    VerificationFailedError with every problem found. Returns the secrets
    checked and the audit rows verified."""
    problems = []
    counts = db_copy.table_counts(target)
    for name, count in expected.items():
        if counts.get(name) != count:
            problems.append(f"table {name} has {counts.get(name)} rows, the source {count}")
    target_revision = _revision(target)
    if target_revision != revision:
        problems.append(f"the target is at revision {target_revision}, the source at {revision}")
    checked, failed = _undecryptable(session, settings.credential_encryption_key)
    if failed:
        problems.append(
            "the current CREDENTIAL_ENCRYPTION_KEY doesn't decrypt: " + ", ".join(failed[:20])
        )
    chain = verify_chain(build_audit_chain_key(settings.credential_encryption_key), session)
    session.flush()
    if not chain.ok:
        problems.append(
            f"the audit chain doesn't verify (first problem at row {chain.broken_row_id}, "
            f"{chain.reason})"
        )
    if problems:
        raise VerificationFailedError(problems)
    return checked, chain.checked_count


def _record(session: Session, settings: Settings, report: MigrationReport) -> None:
    """The audit entry, chained after the copied ones, then the chain
    verified again with it."""
    key = build_audit_chain_key(settings.credential_encryption_key)
    entry = AuditLog(
        user_id=None,
        action=AUDIT_ACTION,
        timestamp=utcnow(),
        event_metadata={
            "from_engine": "sqlite",
            "to_engine": "postgresql",
            "tables": report.tables,
            "rows": report.rows,
            "credentials_checked": report.credentials_checked,
            "audit_rows_verified": report.audit_rows_verified,
            "result": "ok",
        },
    )
    chain_new_entry(key, session, entry)
    session.add(entry)
    session.flush()
    chain = verify_chain(key, session)
    session.flush()
    if not chain.ok:
        raise VerificationFailedError(
            [f"the audit chain doesn't verify with the migration's entry ({chain.reason})"]
        )


def _copy(
    source: Connection, target: Connection, settings: Settings, *, revision: str, dry_run: bool
) -> MigrationReport:
    """Everything that writes, in the target's one transaction."""
    transaction = target.begin()
    try:
        if _revision(target) is None:
            _upgrade_to_head(target)
        _clear_migration_seeds(target)
        expected = db_copy.table_counts(source)
        copied = db_copy.copy_tables(source, target)
        db_copy.reset_sequences(target)
        mismatched = [name for name, count in copied.items() if expected.get(name) != count]
        if mismatched:
            raise VerificationFailedError(
                [f"rows read from {name} changed while it was copied" for name in mismatched]
            )
        with Session(bind=target, join_transaction_mode="rollback_only") as session:
            checked, verified = _verify(
                target, session, settings=settings, expected=expected, revision=revision
            )
            report = MigrationReport(
                tables=copied,
                credentials_checked=checked,
                audit_rows_verified=verified,
                dry_run=dry_run,
                revision=revision,
            )
            _record(session, settings, report)
        if dry_run:
            transaction.rollback()
        else:
            transaction.commit()
        return report
    except BaseException:
        if transaction.is_active:
            transaction.rollback()
        raise


def migrate(source_url: str, *, dry_run: bool, settings: Settings | None = None) -> MigrationReport:
    """Runs the migration; raises MigrationRefusedError (nothing written)
    or VerificationFailedError (rolled back)."""
    settings = settings or get_settings()
    file = _sqlite_file(source_url)
    target_url = settings.database_url
    if not is_postgresql_url(target_url):
        raise MigrationRefusedError(
            "target_not_postgresql",
            "DATABASE_URL must point at the PostgreSQL database to migrate to",
        )
    salt = Path(settings.credential_salt_path)
    if not salt.is_file():
        raise MigrationRefusedError(
            "credential_salt_missing",
            f"the credential salt {salt} is missing: run this with the installation's own "
            "DATA_DIR (or CREDENTIAL_SALT_PATH), or nothing it encrypted can be read",
        )
    try:
        lock = instance_lock.acquire(settings)
    except instance_lock.InstanceLockHeldError as exc:
        raise MigrationRefusedError(
            "instance_running",
            f"PerchTail is running on this DATA_DIR ({exc.holder or exc.content}); stop it first",
        ) from None
    source_engine = _read_only_engine(file)
    target_engine = _target_engine(target_url)
    try:
        head = _head_revision()
        with source_engine.connect() as source:
            revision = _revision(source)
        if revision != head:
            raise MigrationRefusedError(
                "source_revision",
                f"the SQLite database is at revision {revision}, not {head}: start this version "
                "of PerchTail on it once (it migrates the database), stop it, then run this again",
            )
        try:
            check_database_server(target_engine, logger, settings)
        except DatabaseServerRefusedError as exc:
            raise MigrationRefusedError("target_server", str(exc)) from None
        try:
            database_lock = instance_lock.acquire_database(target_engine)
        except instance_lock.DatabaseLockHeldError as exc:
            raise MigrationRefusedError("instance_running", str(exc)) from None
        try:
            with target_engine.connect() as target, source_engine.connect() as source:
                target_revision = _revision(target)
                if target_revision is not None and target_revision != head:
                    raise MigrationRefusedError(
                        "target_revision",
                        f"the PostgreSQL database is at revision {target_revision}, not {head}: "
                        "migrate into a new, empty database",
                    )
                occupied = _occupied_tables(target)
                # Reading only: end the transaction the checks began.
                target.rollback()
                if occupied:
                    raise MigrationRefusedError(
                        "target_not_empty",
                        "the PostgreSQL database already holds data in "
                        + ", ".join(occupied)
                        + ": migrate into a new, empty database (drop and create it again)",
                    )
                return _copy(source, target, settings, revision=head, dry_run=dry_run)
        finally:
            if database_lock is not None:
                database_lock.release()
    finally:
        source_engine.dispose()
        target_engine.dispose()
        lock.release()


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.migrate_database",
        description="Copy a PerchTail SQLite database into the empty PostgreSQL database "
        "DATABASE_URL points at, and check the copy before committing it.",
    )
    parser.add_argument(
        "--from",
        dest="source",
        required=True,
        metavar="URL",
        help="the SQLite database to copy, as sqlite:////data/db/perchtail.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything, including the checks, then roll back",
    )
    args = parser.parse_args(argv)
    configure_logging()
    # Like `python -m app.backup restore`: a lock file created as root
    # would be unusable to the app.
    if _is_root():
        print(f"ERROR: refusing to run as root: {ROOT_REFUSAL}", file=sys.stderr)
        return 2
    settings = get_settings()
    context = {
        "from_engine": "sqlite",
        "to_engine": "postgresql",
        "source": args.source,
        "target": redacted_database_url(settings.database_url),
        "dry_run": args.dry_run,
    }
    try:
        report = migrate(args.source, dry_run=args.dry_run, settings=settings)
    except MigrationRefusedError as exc:
        logger.error(AUDIT_ACTION, **context, result="refused", reason=exc.reason)
        print(f"ERROR: {exc} -- nothing was written.", file=sys.stderr)
        return 1
    except VerificationFailedError as exc:
        logger.error(AUDIT_ACTION, **context, result="failed", problems=exc.problems)
        print(
            "ERROR: the copy didn't check out, so it was rolled back and the PostgreSQL "
            "database is as it was: " + "; ".join(exc.problems),
            file=sys.stderr,
        )
        return 1
    except db_copy.TableCopyError as exc:
        logger.error(
            AUDIT_ACTION,
            **context,
            result="failed",
            error=type(exc).__name__,
            table=exc.table,
            reason=exc.reason,
        )
        print(
            f"ERROR: a row of {exc.table} doesn't fit PostgreSQL: {exc.reason}. The migration "
            "was rolled back; the PostgreSQL database is as it was.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - reported, and the transaction rolled back
        logger.error(AUDIT_ACTION, **context, result="failed", error=type(exc).__name__)
        # A database error is described without its statement's values.
        detail = db_copy.describe_database_error(exc) if isinstance(exc, DBAPIError) else exc
        print(
            f"ERROR: the migration failed and was rolled back ({type(exc).__name__}: {detail}); "
            "the PostgreSQL database is as it was.",
            file=sys.stderr,
        )
        return 1
    logger.info(
        AUDIT_ACTION,
        **context,
        result="dry_run" if report.dry_run else "ok",
        tables=report.tables,
        rows=report.rows,
        credentials_checked=report.credentials_checked,
        audit_rows_verified=report.audit_rows_verified,
        revision=report.revision,
    )
    summary = (
        f"{report.rows} rows in {len(report.tables)} tables, "
        f"{report.credentials_checked} stored secret(s) decrypted, "
        f"{report.audit_rows_verified} audit row(s) verified"
    )
    if report.dry_run:
        print(f"Dry run: {summary}. Rolled back; nothing was written.")
    else:
        print(
            f"Migrated {summary}, to {context['target']}.\n"
            "Next: keep DATABASE_URL pointing at PostgreSQL and start PerchTail. The SQLite "
            "file was not changed: to go back, point DATABASE_URL at it again."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
