"""Copies PerchTail's tables from one database to another (#306): every
table of the models (SQLModel.metadata), with the rows' own ids, in the
caller's transactions. Used by `python -m app.migrate_database` (SQLite to
PostgreSQL).

What is copied is what the models declare, nothing else: not
alembic_version (the target has its own, from its migrations), not SQLite's
FTS5 tables nor PostgreSQL's trigram index (each engine builds its own from
file_name_entry, app/name_index_store.py).

Rows are read and written through the models' column types, so each value
is converted for the target engine (a SQLite boolean 0/1 becomes a
PostgreSQL boolean, a timestamp string a timestamp), and read in batches
from a cursor kept open, never the whole table at once. Tables go in
foreign-key order (sorted_tables). A table that points at itself (a folder
in a folder) is written with those columns empty and filled in once all its
rows are there, since a row may point at one with a higher id. A JSON column
that is NULL stays NULL (not the JSON value null), as in the source."""

from collections.abc import Iterable
from functools import cache

from sqlalchemy import (
    JSON,
    Integer,
    MetaData,
    Table,
    bindparam,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlmodel import SQLModel

from app import models  # noqa: F401  registers tables on SQLModel.metadata
from app.auth import models as auth_models  # noqa: F401  registers tables on SQLModel.metadata

# Rows read from the source and written to the target at a time.
COPY_BATCH_ROWS = 1000

# PostgreSQL errors whose primary message names the table, column, type or
# constraint but never a value (the values, if any, are in its DETAIL).
# Any other error is described by its class and SQLSTATE only.
_VALUE_FREE_SQLSTATES = {
    "22001",  # value too long for type character varying(n)
    "22003",  # integer out of range
    "23502",  # null value in column ... violates not-null constraint
    "23503",  # insert or update on table ... violates foreign key constraint ...
    "23505",  # duplicate key value violates unique constraint ...
    "23514",  # new row for relation ... violates check constraint ...
}


class TableCopyError(Exception):
    """A row of `table` couldn't be written to the target. `reason` says
    why without any of the row's values (they may be secrets or password
    hashes)."""

    def __init__(self, table: str, reason: str):
        self.table = table
        self.reason = reason
        super().__init__(f"a row of {table} couldn't be written: {reason}")


def describe_database_error(exc: DBAPIError) -> str:
    """What went wrong, never with the values of the statement or its rows:
    PostgreSQL's primary message for the errors known to carry no value,
    otherwise the driver error's class and SQLSTATE."""
    original = exc.orig
    sqlstate = getattr(original, "sqlstate", None)
    diag = getattr(original, "diag", None)
    primary = getattr(diag, "message_primary", None)
    if sqlstate in _VALUE_FREE_SQLSTATES and primary:
        return primary
    name = type(original).__name__ if original is not None else type(exc).__name__
    return f"{name} (SQLSTATE {sqlstate})" if sqlstate else name


def app_tables() -> list[Table]:
    """Every table of the models, parents before the tables that point at
    them."""
    return list(SQLModel.metadata.sorted_tables)


def _quote(connection: Connection, name: str) -> str:
    return connection.dialect.identifier_preparer.quote(name)


def existing_tables(connection: Connection) -> list[Table]:
    """The tables of the models that exist in this database."""
    present = set(inspect(connection).get_table_names())
    return [table for table in app_tables() if table.name in present]


def table_counts(connection: Connection, tables: Iterable[Table] | None = None) -> dict[str, int]:
    """Rows per table, for the tables given (every table of the models by
    default; each must exist)."""
    counts = {}
    for table in app_tables() if tables is None else tables:
        counts[table.name] = connection.execute(select(func.count()).select_from(table)).scalar()
    return counts


@cache
def _write_table(table: Table) -> Table:
    """The table as it is written: the same columns, with JSON columns
    writing None as SQL NULL, so a NULL read from the source stays NULL."""
    copy = table.to_metadata(MetaData())
    for column in copy.columns:
        if isinstance(column.type, JSON):
            column.type = JSON(none_as_null=True)
    return copy


def _self_references(table: Table) -> list[str]:
    """The columns through which the table points at itself."""
    names = sorted(
        {key.parent.name for key in table.foreign_keys if key.references(table)},
    )
    for name in names:
        if not table.c[name].nullable:
            raise ValueError(f"{table.name}.{name} points at its own table and isn't nullable")
    return names


def copy_table(
    source: Connection,
    target: Connection,
    table: Table,
    *,
    batch_rows: int = COPY_BATCH_ROWS,
) -> int:
    """Copies one table's rows, in primary-key order, and returns how many.
    A row the target refuses raises TableCopyError, which names the table
    and the kind of error but none of the values."""
    try:
        return _copy_table(source, target, table, batch_rows=batch_rows)
    except DBAPIError as exc:
        if exc.connection_invalidated:
            raise
        raise TableCopyError(table.name, describe_database_error(exc)) from None


def _copy_table(source: Connection, target: Connection, table: Table, *, batch_rows: int) -> int:
    written = _write_table(table)
    later = _self_references(table)
    primary_key = [column.name for column in table.primary_key.columns]
    pending: list[dict] = []
    copied = 0
    result = source.execution_options(yield_per=batch_rows).execute(
        select(table).order_by(*table.primary_key.columns)
    )
    for partition in result.mappings().partitions(batch_rows):
        rows = [dict(row) for row in partition]
        for row in rows:
            values = {name: row[name] for name in later if row[name] is not None}
            if values:
                pending.append({**{f"pk_{name}": row[name] for name in primary_key}, **values})
                for name in values:
                    row[name] = None
        target.execute(insert(written), rows)
        copied += len(rows)
    for name in later:
        filled = [row for row in pending if name in row]
        if not filled:
            continue
        # Bound names that aren't column names: SQLAlchemy keeps those for SET.
        statement = (
            update(written)
            .where(*(written.c[key] == bindparam(f"pk_{key}") for key in primary_key))
            .values({name: bindparam(f"v_{name}")})
        )
        target.execute(
            statement,
            [
                {**{f"pk_{key}": row[f"pk_{key}"] for key in primary_key}, f"v_{name}": row[name]}
                for row in filled
            ],
        )
    return copied


def copy_tables(
    source: Connection, target: Connection, *, batch_rows: int = COPY_BATCH_ROWS
) -> dict[str, int]:
    """Copies every table of the models, in foreign-key order, into the
    target's open transaction, and returns the rows copied per table."""
    return {
        table.name: copy_table(source, target, table, batch_rows=batch_rows)
        for table in app_tables()
    }


def reset_sequences(target: Connection) -> None:
    """PostgreSQL: moves each table's id sequence past the highest id copied,
    so the next row gets a new id and none is ever reused. Nothing to do on
    SQLite, where AUTOINCREMENT follows the ids written."""
    if target.dialect.name != "postgresql":
        return
    for table in app_tables():
        columns = list(table.primary_key.columns)
        if len(columns) != 1 or not isinstance(columns[0].type, Integer):
            continue  # a composite or text key has no sequence
        quoted_table = _quote(target, table.name)
        quoted_column = _quote(target, columns[0].name)
        # pg_get_serial_sequence is NULL for an integer key without a
        # sequence, and setval(NULL, ...) is NULL too. Names from the
        # models, quoted.
        target.execute(
            text(
                "SELECT setval(pg_get_serial_sequence(:table, :column), "  # nosec B608
                f"COALESCE((SELECT max({quoted_column}) FROM {quoted_table}), 0) + 1, false)"
            ),
            {"table": quoted_table, "column": columns[0].name},
        )
