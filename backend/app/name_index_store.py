"""Storage and search for the file-name index (#304): the SQLite-specific
part, kept in one module so another database (PostgreSQL, #306) only needs
its own version of these functions.

`file_name_entry` (app/models.py FileNameEntry) holds one row per file; the
`file_name_fts` FTS5 table indexes its `path` with the trigram tokenizer,
which answers "contains this text" without reading every row. Triggers keep
the two in step, so code only ever writes `file_name_entry`.

Matching follows the explorer's filter (frontend/src/lib/explorer-scope.ts
compileMatcher), always case-insensitive:
- text: found anywhere in the file name;
- a glob (`*`, `?`): the whole name, `*` within a folder, `**` across;
- a regex: found anywhere in the name.
A pattern containing `/` is matched against the file's path in the source
instead of its name."""

import re
from collections.abc import Callable
from dataclasses import dataclass

import re2
from sqlalchemy import text
from sqlmodel import Session

from app.rules import compile_linear

FTS_TABLE = "file_name_fts"
# The DDL spells the table name out (no string building), like the migration.
_SCHEMA = (
    "CREATE VIRTUAL TABLE file_name_fts USING fts5("
    "path, content='file_name_entry', content_rowid='id', tokenize='trigram')",
    "CREATE TRIGGER file_name_entry_ai AFTER INSERT ON file_name_entry BEGIN "
    "INSERT INTO file_name_fts(rowid, path) VALUES (new.id, new.path); END",
    "CREATE TRIGGER file_name_entry_ad AFTER DELETE ON file_name_entry BEGIN "
    "INSERT INTO file_name_fts(file_name_fts, rowid, path) "
    "VALUES ('delete', old.id, old.path); END",
    "CREATE TRIGGER file_name_entry_au AFTER UPDATE OF path ON file_name_entry BEGIN "
    "INSERT INTO file_name_fts(file_name_fts, rowid, path) VALUES ('delete', old.id, old.path); "
    "INSERT INTO file_name_fts(rowid, path) VALUES (new.id, new.path); END",
    # Rows that were already there (an upgrade of a populated table).
    "INSERT INTO file_name_fts(file_name_fts) VALUES ('rebuild')",
)
# Trigrams need three characters; shorter text is matched without the index
# (reading every row: still quick, and right for any letter case).
_TRIGRAM = 3


def ensure_name_index_schema(target_engine) -> None:
    """Creates the FTS5 table and its sync triggers if missing. Like
    db.ensure_search_schema, FTS5 tables can't be declared as SQLModel
    tables, so startup, the migration and the tests' session fixture call
    this after create_all()."""
    with target_engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"), {"n": FTS_TABLE}
        ).scalar()
        if exists:
            return
        for statement in _SCHEMA:
            conn.execute(text(statement))


class InvalidPattern(ValueError):
    pass


@dataclass(frozen=True)
class Matcher:
    by_path: bool
    regex: object | None  # a compiled RE2 or re pattern; None: plain text
    needle: str
    # The longest piece of text every match contains, to narrow the search
    # with the trigram index first ("" = no narrowing).
    literal: str

    def matches(self, path: str) -> bool:
        target = path if self.by_path else path.rsplit("/", 1)[-1]
        if self.regex is None:
            return self.needle in target.casefold()
        return self.regex.search(target) is not None


def _glob_to_regex(glob: str) -> str:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def compile_matcher(query: str, regex: bool) -> Matcher | None:
    """None for an empty query (everything matches); InvalidPattern for a
    regex that doesn't compile."""
    q = query.strip()
    if not q:
        return None
    by_path = "/" in q
    if regex:
        # RE2 runs in linear time, so no pattern can make the search hang.
        try:
            return Matcher(by_path, compile_linear(q, ignore_case=True), "", "")
        except re2.error as exc:
            raise InvalidPattern(str(exc)) from exc
    if re.search(r"[*?]", q):
        pieces = [p for p in re.split(r"[*?]+", q) if p]
        literal = max(pieces, key=len) if pieces else ""
        return Matcher(by_path, re.compile(f"^{_glob_to_regex(q)}$", re.IGNORECASE), "", literal)
    return Matcher(by_path, None, q.casefold(), q)


def _fts_phrase(literal: str) -> str:
    return '"' + literal.replace('"', '""') + '"'


def search_entries(
    session: Session,
    matcher: Matcher | None,
    source_ids: set[int] | None,
    after_id: int,
    limit: int,
    visible: Callable[[int, str], bool] | None = None,
) -> tuple[list[tuple[int, int, str, int, float | None]], int | None]:
    """Matching entries (id, source_id, path, size, mtime) with an id above
    `after_id`, at most `limit`, in id order, and the id to continue after
    (None when no other match follows). `source_ids` None means every source.
    Rows are read in batches and filtered here, so there's no cap on how
    many match -- only on how many come back per call. `visible(source_id,
    path)` drops rows the source's rules hide now; it's checked here, inside
    the paging, so the returned cursor never points past a hidden row (which
    would tell the caller one matched)."""
    if source_ids is not None and not source_ids:
        return [], None
    params: dict = {"after": after_id}
    clauses = ["e.id > :after"]
    if source_ids is not None:
        ids = sorted(source_ids)
        names = [f"s{i}" for i in range(len(ids))]
        clauses.append(f"e.source_id IN ({', '.join(':' + n for n in names)})")
        params.update(dict(zip(names, ids, strict=True)))
    join = ""
    if matcher is not None and len(matcher.literal) >= _TRIGRAM:
        join = f"JOIN {FTS_TABLE} f ON f.rowid = e.id AND f.path MATCH :phrase"
        params["phrase"] = _fts_phrase(matcher.literal)

    batch = max(limit * 4, 500)
    # Built from the fixed clauses above and an int; every value from the
    # caller is a bound :parameter.
    where = " AND ".join(clauses)
    query = text(
        "SELECT e.id, e.source_id, e.path, e.size, e.mtime "  # nosec B608
        f"FROM file_name_entry e {join} WHERE {where} ORDER BY e.id LIMIT {batch}"
    )
    found: list[tuple[int, int, str, int, float | None]] = []
    while True:
        rows = session.execute(query, params).all()
        for row in rows:
            if (matcher is None or matcher.matches(row[2])) and (
                visible is None or visible(row[1], row[2])
            ):
                found.append(tuple(row))
                # One past the page: a cursor only when another match follows.
                if len(found) > limit:
                    return found[:limit], found[limit - 1][0]
        if len(rows) < batch:
            return found, None
        params["after"] = rows[-1][0]
