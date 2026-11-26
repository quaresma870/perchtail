"""app/db_copy.py (#306): every table of the models copied with its ids, in
batches, a folder inside a newer folder included, JSON NULL kept NULL, and
on PostgreSQL the id sequences moved past the copied ids."""

import os

import pytest
from app import db_copy
from app.auth.models import AuditLog
from app.config import normalize_database_url
from app.models import Customer, FileNameEntry, Folder
from app.name_index_store import ensure_name_index_schema
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, select

from tests.migration_seed import BIG, INDEXED_FILES, populate

TEST_DATABASE_URL = normalize_database_url(os.environ.get("TEST_DATABASE_URL", ""))


def _sqlite(path, *, foreign_keys: bool = False):
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    if foreign_keys:

        @event.listens_for(engine, "connect")
        def _on(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    ensure_name_index_schema(engine)
    return engine


def _populated_source(tmp_path):
    engine = _sqlite(tmp_path / "source.db")
    with Session(engine) as session:
        seeded = populate(session, engine)
    return engine, seeded


def _copy(source_engine, target_engine, **options) -> dict[str, int]:
    with source_engine.connect() as source, target_engine.connect() as target:
        with target.begin():
            copied = db_copy.copy_tables(source, target, **options)
            db_copy.reset_sequences(target)
    return copied


def _target(tmp_path, request):
    if request.param == "postgresql":
        url = request.getfixturevalue("empty_postgres_url")
        engine = create_engine(url, poolclass=NullPool)
        SQLModel.metadata.create_all(engine)
        ensure_name_index_schema(engine)
        return engine
    # SQLite with its foreign keys enforced, as PostgreSQL always does.
    return _sqlite(tmp_path / "target.db", foreign_keys=True)


@pytest.fixture(
    params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres_only)],
)
def target_engine(tmp_path, request):
    engine = _target(tmp_path, request)
    yield engine
    engine.dispose()


def test_every_table_is_copied_with_its_ids(tmp_path, target_engine):
    source_engine, seeded = _populated_source(tmp_path)
    with source_engine.connect() as source:
        expected = db_copy.table_counts(source)

    copied = _copy(source_engine, target_engine, batch_rows=97)

    assert copied == expected
    assert set(copied) == {table.name for table in SQLModel.metadata.sorted_tables}
    assert copied["file_name_entry"] == INDEXED_FILES
    with target_engine.connect() as target:
        assert db_copy.table_counts(target) == expected
    with Session(source_engine) as source, Session(target_engine) as target:
        for model in (Customer, Folder, FileNameEntry, AuditLog):
            mine = [row.model_dump() for row in source.exec(select(model).order_by(model.id))]
            theirs = [row.model_dump() for row in target.exec(select(model).order_by(model.id))]
            assert theirs == mine, model.__name__
        child = target.get(Folder, seeded["child_id"])
        assert child.parent_folder_id == seeded["parent_id"] > child.id
        biggest = target.exec(select(FileNameEntry).order_by(FileNameEntry.id.desc())).first()
        assert biggest.size == BIG + INDEXED_FILES - 1
    source_engine.dispose()


def test_a_json_null_stays_null(tmp_path, target_engine):
    source_engine = _sqlite(tmp_path / "source.db")
    with Session(source_engine) as session:
        session.add(Customer(name="c"))
        session.commit()
        session.execute(
            text(
                "INSERT INTO auditlog (user_id, action, timestamp, metadata) "
                "VALUES (NULL, 'x', '2026-01-01 00:00:00.000000', NULL)"
            )
        )
        session.commit()

    _copy(source_engine, target_engine)

    with target_engine.connect() as target:
        assert target.execute(text("SELECT count(*) FROM auditlog WHERE metadata IS NULL")).scalar()
        assert target.execute(text("SELECT count(*) FROM auditlog")).scalar() == 1
    source_engine.dispose()


def test_a_folder_pointing_at_a_missing_folder_fails_the_copy(tmp_path, target_engine):
    """The foreign keys still hold: a row that points nowhere isn't copied."""
    source_engine = _sqlite(tmp_path / "source.db")
    with Session(source_engine) as session:
        customer = Customer(name="c")
        session.add(customer)
        session.flush()
        session.add(Folder(name="orphan", customer_id=customer.id, parent_folder_id=999))
        session.commit()

    # PostgreSQL's message names the constraint; SQLite's error is named by
    # its class. Neither carries the row's values.
    with pytest.raises(db_copy.TableCopyError, match="(?i)foreign key|IntegrityError") as failed:
        _copy(source_engine, target_engine)
    assert failed.value.table == "folder"
    assert "orphan" not in str(failed.value) and "999" not in str(failed.value)
    with target_engine.connect() as target:
        assert db_copy.table_counts(target)["folder"] == 0
    source_engine.dispose()


@pytest.mark.postgres_only
def test_the_next_row_after_the_copy_gets_a_new_id(tmp_path, empty_postgres_url):
    source_engine, _ = _populated_source(tmp_path)
    target_engine = create_engine(empty_postgres_url, poolclass=NullPool)
    SQLModel.metadata.create_all(target_engine)
    ensure_name_index_schema(target_engine)
    try:
        _copy(source_engine, target_engine)
        with Session(source_engine) as source:
            highest_folder = source.exec(select(Folder.id).order_by(Folder.id.desc())).first()
            highest_audit = source.exec(select(AuditLog.id).order_by(AuditLog.id.desc())).first()
        with Session(target_engine) as target:
            customer = target.exec(select(Customer)).first()
            folder = Folder(name="new", customer_id=customer.id)
            entry = AuditLog(action="x")
            target.add(folder)
            target.add(entry)
            target.commit()
            assert folder.id == highest_folder + 1
            assert entry.id == highest_audit + 1
    finally:
        target_engine.dispose()
        source_engine.dispose()


@pytest.mark.postgres_only
def test_an_empty_table_starts_its_ids_at_one(tmp_path, empty_postgres_url):
    source_engine = _sqlite(tmp_path / "source.db")
    target_engine = create_engine(empty_postgres_url, poolclass=NullPool)
    SQLModel.metadata.create_all(target_engine)
    try:
        _copy(source_engine, target_engine)
        with Session(target_engine) as target:
            customer = Customer(name="first")
            target.add(customer)
            target.commit()
            assert customer.id == 1
    finally:
        target_engine.dispose()
        source_engine.dispose()
