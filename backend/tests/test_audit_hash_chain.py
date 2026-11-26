import pytest
from app.audit_hash_chain import (
    backfill_chain_if_needed,
    chain_new_entry,
    compute_row_hash,
    current_chain_tip,
    unsigned_row_ids,
)
from app.auth.models import AuditChainState, AuditLog
from app.timeutils import utcnow
from sqlmodel import select

# A fixed, arbitrary key -- these tests exercise the hashing/chaining logic
# itself, not app.crypto's key derivation (that's test_crypto.py's job), so
# there's no need to touch CREDENTIAL_ENCRYPTION_KEY/the salt file here.
KEY = b"unit-test-key-not-a-real-secret"
OTHER_KEY = b"a-different-unit-test-key"


def test_compute_row_hash_is_deterministic():
    ts = utcnow()
    kwargs = dict(
        user_id=1,
        action="source.create",
        target_type="source",
        target_id=5,
        timestamp=ts,
        metadata={"name": "app01"},
    )
    assert compute_row_hash(KEY, "abc", **kwargs) == compute_row_hash(KEY, "abc", **kwargs)


def test_compute_row_hash_changes_with_prev_hash():
    ts = utcnow()
    kwargs = dict(
        user_id=1, action="login", target_type=None, target_id=None, timestamp=ts, metadata=None
    )
    assert compute_row_hash(KEY, None, **kwargs) != compute_row_hash(
        KEY, "something-else", **kwargs
    )


def test_compute_row_hash_changes_with_any_field():
    ts = utcnow()
    base = dict(
        key=KEY,
        prev_hash=None,
        user_id=1,
        action="login",
        target_type=None,
        target_id=None,
        timestamp=ts,
        metadata=None,
    )
    baseline = compute_row_hash(**base)
    for field, value in [("user_id", 2), ("action", "logout"), ("target_type", "source")]:
        variant = dict(base)
        variant[field] = value
        assert compute_row_hash(**variant) != baseline


def test_compute_row_hash_changes_with_the_key():
    # The whole point of keying the chain (see this module's own docstring):
    # the same content chained under two different keys must produce two
    # different hashes, or the key isn't actually participating in the
    # computation -- a plain, keyless hash of public fields could be
    # recomputed by anyone with just the SQLite file, silently defeating
    # tamper-evidence against exactly the threat this feature targets.
    ts = utcnow()
    kwargs = dict(
        user_id=1, action="login", target_type=None, target_id=None, timestamp=ts, metadata=None
    )
    assert compute_row_hash(KEY, None, **kwargs) != compute_row_hash(OTHER_KEY, None, **kwargs)


def test_current_chain_tip_is_none_for_an_empty_chain(session):
    assert current_chain_tip(session) is None


def test_current_chain_tip_is_the_last_rows_hash(session):
    first = AuditLog(user_id=None, action="a")
    chain_new_entry(KEY, session, first)
    session.add(first)
    session.commit()

    second = AuditLog(user_id=None, action="b")
    chain_new_entry(KEY, session, second)
    session.add(second)
    session.commit()

    assert current_chain_tip(session) == second.row_hash
    assert second.prev_hash == first.row_hash


def test_current_chain_tip_falls_back_to_anchor_when_no_rows_remain(session):
    session.add(AuditChainState(anchor_row_id=9, anchor_hash="anchor-hash"))
    session.commit()

    assert current_chain_tip(session) == "anchor-hash"


def test_chain_new_entry_sees_uncommitted_prior_entry_in_same_session(session):
    # Regression guard: two record_audit_event-style calls in one
    # transaction must chain relative to each other, not both compute the
    # same prev_hash by missing the first one's not-yet-committed row.
    first = AuditLog(user_id=None, action="a")
    chain_new_entry(KEY, session, first)
    session.add(first)

    second = AuditLog(user_id=None, action="b")
    chain_new_entry(KEY, session, second)
    session.add(second)

    session.commit()
    assert second.prev_hash == first.row_hash
    assert first.row_hash != second.row_hash


def test_backfill_chains_rows_missing_a_hash_in_id_order(session):
    rows = [AuditLog(user_id=None, action=f"legacy.{i}") for i in range(3)]
    for row in rows:
        session.add(row)
    session.commit()
    for row in rows:
        assert row.row_hash is None

    backfilled = backfill_chain_if_needed(KEY, session)
    session.commit()

    assert backfilled == 3
    session.refresh(rows[0])
    session.refresh(rows[1])
    session.refresh(rows[2])
    assert rows[0].prev_hash is None
    assert rows[1].prev_hash == rows[0].row_hash
    assert rows[2].prev_hash == rows[1].row_hash
    assert len({r.row_hash for r in rows}) == 3


def test_an_unsigned_row_after_a_purge_anchor_is_not_signed(session):
    # A purge only ever runs on a signed chain, so an anchor means the
    # chain has started: a row without a hash is not a legacy one.
    session.add(AuditChainState(anchor_row_id=9, anchor_hash="anchor-hash"))
    row = AuditLog(user_id=None, action="legacy.0")
    session.add(row)
    session.commit()

    assert backfill_chain_if_needed(KEY, session) == 0
    session.commit()
    session.refresh(row)
    assert row.row_hash is None


def test_an_unsigned_row_inserted_after_the_chain_started_is_not_signed(session):
    first = AuditLog(user_id=None, action="a")
    chain_new_entry(KEY, session, first)
    session.add(first)
    session.commit()
    forged = AuditLog(user_id=None, action="forged")
    session.add(forged)
    session.commit()

    assert backfill_chain_if_needed(KEY, session) == 0
    session.commit()
    session.refresh(forged)
    assert forged.row_hash is None
    assert unsigned_row_ids(session) == [forged.id]
    # The next entry chains past it, from the last signed row.
    after = AuditLog(user_id=None, action="b")
    chain_new_entry(KEY, session, after)
    assert after.prev_hash == first.row_hash


def test_a_chain_whose_tail_lost_its_hashes_is_never_signed_again(session):
    # Even without the marker in DATA_DIR: the first row is still signed,
    # so the chain has started and nothing after it is signed late.
    rows = [AuditLog(user_id=None, action=f"a.{i}") for i in range(4)]
    for row in rows:
        chain_new_entry(KEY, session, row)
        session.add(row)
        session.commit()
    for row in rows[1:]:
        row.prev_hash = None
        row.row_hash = None
        session.add(row)
    session.commit()

    assert backfill_chain_if_needed(KEY, session) == 0
    session.commit()
    assert unsigned_row_ids(session) == [row.id for row in rows[1:]]


def test_backfill_is_a_no_op_when_nothing_needs_it(session):
    row = AuditLog(user_id=None, action="a")
    chain_new_entry(KEY, session, row)
    session.add(row)
    session.commit()

    assert backfill_chain_if_needed(KEY, session) == 0


def test_backfill_twice_is_idempotent(session):
    # app.main's lifespan calls this on every single startup, not just the
    # first one after upgrading -- a second (or hundredth) call once
    # everything's already backfilled must be a true no-op, not rehash
    # (and therefore silently change) rows a prior run already chained.
    for i in range(3):
        session.add(AuditLog(user_id=None, action=f"legacy.{i}"))
    session.commit()

    first_pass = backfill_chain_if_needed(KEY, session)
    session.commit()
    assert first_pass == 3
    hashes_after_first_pass = [
        row.row_hash for row in session.exec(select(AuditLog).order_by(AuditLog.id)).all()
    ]

    second_pass = backfill_chain_if_needed(KEY, session)
    session.commit()
    assert second_pass == 0
    hashes_after_second_pass = [
        row.row_hash for row in session.exec(select(AuditLog).order_by(AuditLog.id)).all()
    ]

    assert hashes_after_first_pass == hashes_after_second_pass


def test_concurrent_writers_never_chain_from_the_same_tip(tmp_path):
    """#213: a second transaction could read the tip while the first one's
    row was still uncommitted, so both chained from the same hash and the
    log looked tampered with. The second writer now waits for the first."""
    import threading
    import time

    from app.audit_integrity import verify_chain
    from app.db import configure_sqlite_connection
    from sqlalchemy import event
    from sqlmodel import Session, SQLModel, create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / 'audit.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    event.listen(engine, "connect", configure_sqlite_connection)
    SQLModel.metadata.create_all(engine)

    def entry(action):
        return AuditLog(user_id=None, action=action, timestamp=utcnow())

    first = Session(engine)
    row = entry("first")
    chain_new_entry(KEY, first, row)
    first.add(row)
    first.flush()  # written, not committed

    done = threading.Event()

    def second_writer():
        with Session(engine) as second:
            other = entry("second")
            chain_new_entry(KEY, second, other)
            second.add(other)
            second.commit()
        done.set()

    worker = threading.Thread(target=second_writer)
    worker.start()
    time.sleep(0.3)
    assert not done.is_set(), "the second writer must wait for the first to commit"
    first.commit()
    first.close()
    worker.join(timeout=10)
    assert done.is_set()

    with Session(engine) as check:
        result = verify_chain(KEY, check)
    assert result.ok and result.checked_count == 2


def _postgres_engines(session, count):
    """`count` engines of their own on the test database, as separate
    PerchTail processes on one database would have."""
    from sqlmodel import create_engine

    return [create_engine(session.get_bind().url) for _ in range(count)]


@pytest.mark.postgres_only
def test_a_second_postgresql_writer_waits_for_the_first_to_commit(session):
    """The advisory lock is per transaction and per database: a writer on
    another connection, as another process would be, waits for it."""
    import threading
    import time

    from app.audit_integrity import verify_chain
    from sqlmodel import Session

    first_engine, second_engine = _postgres_engines(session, 2)
    first = Session(first_engine)
    try:
        row = AuditLog(user_id=None, action="first", timestamp=utcnow())
        chain_new_entry(KEY, first, row)
        first.add(row)
        first.flush()  # written, not committed

        done = threading.Event()

        def second_writer():
            with Session(second_engine) as second:
                other = AuditLog(user_id=None, action="second", timestamp=utcnow())
                chain_new_entry(KEY, second, other)
                second.add(other)
                second.commit()
            done.set()

        worker = threading.Thread(target=second_writer)
        worker.start()
        time.sleep(0.5)
        assert not done.is_set(), "the second writer must wait for the first to commit"
        first.commit()
        worker.join(timeout=10)
        assert done.is_set()

        result = verify_chain(KEY, session)
        assert result.ok and result.checked_count == 2
        actions = [r.action for r in session.exec(select(AuditLog).order_by(AuditLog.id)).all()]
        assert actions == ["first", "second"]
    finally:
        first.close()  # also when it failed before its commit, so nothing stays locked
        first_engine.dispose()
        second_engine.dispose()


@pytest.mark.postgres_only
def test_a_rolled_back_postgresql_write_leaves_a_gap_verification_accepts(session):
    """A sequence value drawn by a write that is rolled back is never given
    out again: the ids have a gap, and the chain, which follows prev_hash in
    id order, doesn't mind it."""
    from app.audit_integrity import verify_chain

    def write(action):
        entry = AuditLog(user_id=None, action=action, timestamp=utcnow())
        chain_new_entry(KEY, session, entry)
        session.add(entry)
        session.flush()
        return entry.id

    first = write("kept")
    session.commit()
    write("rolled back")
    session.rollback()
    last = write("after")
    session.commit()

    assert last == first + 2
    result = verify_chain(KEY, session)
    assert result.ok and result.checked_count == 2


@pytest.mark.postgres_only
def test_postgresql_writers_in_parallel_keep_one_unbroken_chain(session):
    """Two connections, each its own engine, write 150 audit entries each at
    once, one transaction per entry: verification finds one chain of 300
    rows, in id order, with no break."""
    import threading

    from app.audit_integrity import verify_chain
    from sqlmodel import Session

    per_writer = 150
    engines = _postgres_engines(session, 2)
    start = threading.Barrier(len(engines))
    errors = []

    def writer(index, engine):
        try:
            start.wait()
            for n in range(per_writer):
                with Session(engine) as writing:
                    entry = AuditLog(user_id=None, action=f"w{index}.{n}", timestamp=utcnow())
                    chain_new_entry(KEY, writing, entry)
                    writing.add(entry)
                    writing.commit()
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)

    workers = [
        threading.Thread(target=writer, args=(index, engine))
        for index, engine in enumerate(engines)
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=120)
    finally:
        for engine in engines:
            engine.dispose()

    assert errors == []
    result = verify_chain(KEY, session)
    assert result.ok, result
    assert result.checked_count == 2 * per_writer
    rows = session.exec(select(AuditLog).order_by(AuditLog.id)).all()
    assert [row.prev_hash for row in rows[1:]] == [row.row_hash for row in rows[:-1]]
