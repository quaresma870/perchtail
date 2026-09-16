from app.audit_hash_chain import (
    backfill_chain_if_needed,
    chain_new_entry,
    compute_row_hash,
    current_chain_tip,
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


def test_backfill_starts_from_an_existing_anchor_if_one_exists(session):
    session.add(AuditChainState(anchor_row_id=9, anchor_hash="anchor-hash"))
    row = AuditLog(user_id=None, action="legacy.0")
    session.add(row)
    session.commit()

    backfill_chain_if_needed(KEY, session)
    session.commit()
    session.refresh(row)

    assert row.prev_hash == "anchor-hash"


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
