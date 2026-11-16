"""Live alerts (#305) and their webhook delivery (#328)."""

import base64
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app import alerts
from app.alerts import check_alert, deliver_due, post_webhook, queue_delivery, run_alert_pass
from app.auth.models import Capability, Role, ScopeType, User
from app.auth.rbac import create_role_grant
from app.crypto import encrypt_secret
from app.models import (
    Alert,
    AlertDelivery,
    AlertFileState,
    Customer,
    FileNameEntry,
    Folder,
    PatternKind,
    Protocol,
    Rule,
    RuleType,
    Source,
)
from app.timeutils import utcnow
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


@pytest.fixture(autouse=True)
def _no_unreadable_files_from_other_tests():
    # Ids start again in each test's database.
    alerts._unreadable.clear()
    yield
    alerts._unreadable.clear()


def _user(session, *, is_super_admin=True, active=True, manage_alerts=True) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=is_super_admin,
        global_capabilities=["manage_alerts"] if manage_alerts else [],
    )
    session.add(role)
    session.commit()
    user = User(username=f"user-{uuid.uuid4().hex[:8]}", role_id=role.id, active=active)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _source(session, base, *, name=None, **kw) -> Source:
    source = Source(
        name=name or f"src-{uuid.uuid4().hex[:6]}",
        protocol=Protocol.local,
        host="localhost",
        base_path=str(base),
        **kw,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    return source


def _write(base, rel, text, mode="w"):
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, newline="") as f:
        f.write(text)
    return path


def _index(session, source, base, *rels):
    """What the file-name index would list (its sizes as of now)."""
    for rel in rels:
        session.add(
            FileNameEntry(
                source_id=source.id,
                path=rel,
                size=(base / rel).stat().st_size,
                seen_at=utcnow(),
            )
        )
    session.commit()


def _alert(session, user, **overrides) -> Alert:
    values = dict(
        user_id=user.id,
        name="my alert",
        query="refused",
        webhook_url_encrypted=encrypt_secret("https://example.com/hook"),
        created_at=utcnow(),
    )
    values.update(overrides)
    alert = Alert(**values)
    session.add(alert)
    session.commit()
    session.refresh(alert)
    return alert


def _lines(hits):
    return [h.line for h in hits]


# --- reading only what was appended -------------------------------------------


def test_a_new_alert_starts_at_the_end_and_then_sees_only_new_lines(session, tmp_path):
    user = _user(session)
    source = _source(session, tmp_path)
    _write(tmp_path, "app.log", "connection refused (old)\n")
    _index(session, source, tmp_path, "app.log")
    alert = _alert(session, user)

    assert check_alert(session, alert) == []  # first pass: positions start at the end
    session.commit()
    _write(tmp_path, "app.log", "ok\nconnection REFUSED again\n", mode="a")
    hits = check_alert(session, alert)
    session.commit()
    assert _lines(hits) == ["connection REFUSED again"]
    assert hits[0].file_path == "app.log" and hits[0].source_name == source.name
    assert check_alert(session, alert) == []  # caught up


def test_a_line_still_being_written_waits_for_its_newline(session, tmp_path):
    user = _user(session)
    source = _source(session, tmp_path)
    _write(tmp_path, "app.log", "")
    _index(session, source, tmp_path, "app.log")
    alert = _alert(session, user)
    check_alert(session, alert)
    session.commit()

    _write(tmp_path, "app.log", "half a refused li", mode="a")
    assert check_alert(session, alert) == []
    session.commit()
    _write(tmp_path, "app.log", "ne\n", mode="a")
    assert _lines(check_alert(session, alert)) == ["half a refused line"]


def test_a_rotated_file_is_read_from_the_top(session, tmp_path):
    user = _user(session)
    source = _source(session, tmp_path)
    _write(tmp_path, "app.log", "x" * 200 + "\n")
    _index(session, source, tmp_path, "app.log")
    alert = _alert(session, user)
    check_alert(session, alert)
    session.commit()

    _write(tmp_path, "app.log", "refused after rotation\n")  # shorter: truncated
    assert _lines(check_alert(session, alert)) == ["refused after rotation"]


def test_a_rewritten_file_of_the_same_length_is_noticed_by_its_start(session, tmp_path):
    user = _user(session)
    source = _source(session, tmp_path)
    _write(tmp_path, "app.log", "aaaa\n")
    _index(session, source, tmp_path, "app.log")
    alert = _alert(session, user)
    check_alert(session, alert)
    session.commit()
    _write(tmp_path, "app.log", "more\n", mode="a")
    check_alert(session, alert)  # records the head of the file
    session.commit()

    _write(tmp_path, "app.log", "refused! new file with other bytes\n")
    assert _lines(check_alert(session, alert)) == ["refused! new file with other bytes"]


def test_regex_queries_use_re2_and_text_ignores_case(session, tmp_path):
    user = _user(session)
    source = _source(session, tmp_path)
    _write(tmp_path, "app.log", "")
    _index(session, source, tmp_path, "app.log")
    alert = _alert(session, user, query=r"code=5\d\d", regex=True)
    check_alert(session, alert)
    session.commit()
    _write(tmp_path, "app.log", "code=404\ncode=503 upstream\n", mode="a")
    assert _lines(check_alert(session, alert)) == ["code=503 upstream"]


# --- which files an alert watches ---------------------------------------------


def test_the_glob_picks_files_and_archives_are_never_read(session, tmp_path):
    user = _user(session)
    source = _source(session, tmp_path)
    for rel in ("app.log", "nested/db.log", "notes.txt", "old.log.gz"):
        _write(tmp_path, rel, "")
    _index(session, source, tmp_path, "app.log", "nested/db.log", "notes.txt", "old.log.gz")
    alert = _alert(session, user)
    check_alert(session, alert)
    session.commit()
    watched = {
        s.file_path
        for s in session.exec(
            select(AlertFileState).where(AlertFileState.alert_id == alert.id)
        ).all()
    }
    assert watched == {"app.log", "nested/db.log"}


def test_scope_by_folder_covers_its_subfolders_and_by_customer(session, tmp_path):
    user = _user(session)
    customer = Customer(name=f"c-{uuid.uuid4().hex[:6]}")
    session.add(customer)
    session.commit()
    parent = Folder(name="eu", customer_id=customer.id)
    session.add(parent)
    session.commit()
    child = Folder(name="prod", customer_id=customer.id, parent_folder_id=parent.id)
    session.add(child)
    session.commit()
    inside = _source(session, tmp_path / "a", customer_id=customer.id, folder_id=child.id)
    outside = _source(session, tmp_path / "b")

    by_folder = _alert(session, user, folder_id=parent.id)
    assert alerts.scope_source_ids(session, user, by_folder) == {inside.id}
    by_customer = _alert(session, user, customer_id=customer.id)
    assert alerts.scope_source_ids(session, user, by_customer) == {inside.id}
    everywhere = _alert(session, user)
    assert {inside.id, outside.id} <= alerts.scope_source_ids(session, user, everywhere)


def test_disabled_sources_and_lost_access_are_left_out(session, tmp_path):
    user = _user(session, is_super_admin=False)
    granted = _source(session, tmp_path / "a")
    hidden = _source(session, tmp_path / "b")
    off = _source(session, tmp_path / "c", enabled=False)
    for source in (granted, off):
        create_role_grant(
            session,
            actor_user_id=None,
            role_id=user.role_id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=[Capability.view],
        )
    session.commit()
    alert = _alert(session, user)
    assert alerts.scope_source_ids(session, user, alert) == {granted.id}
    assert hidden.id not in alerts.scope_source_ids(session, user, alert)


@pytest.mark.parametrize("active,manage", [(False, True), (True, False)])
def test_an_owner_who_may_not_alert_reads_nothing(session, tmp_path, active, manage):
    user = _user(session, active=active, manage_alerts=manage, is_super_admin=False)
    source = _source(session, tmp_path)
    create_role_grant(
        session,
        actor_user_id=None,
        role_id=user.role_id,
        scope_type=ScopeType.source,
        scope_id=source.id,
        capabilities=[Capability.view],
    )
    session.commit()
    _write(tmp_path, "app.log", "refused\n")
    _index(session, source, tmp_path, "app.log")
    alert = _alert(session, user)
    assert check_alert(session, alert) == []
    assert session.exec(select(AlertFileState)).all() == []


def test_files_that_dont_fit_wait_for_the_next_pass(session, tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "FILES_PER_SOURCE", 2)
    user = _user(session)
    source = _source(session, tmp_path)
    names = [f"f{i}.log" for i in range(5)]
    for name in names:
        _write(tmp_path, name, "")
    _index(session, source, tmp_path, *names)
    alert = _alert(session, user)
    check_alert(session, alert)
    session.commit()
    for name in names:
        _write(tmp_path, name, f"refused in {name}\n", mode="a")

    stats = alerts.PassStats()
    seen = set()
    for _ in range(3):
        seen.update(h.file_path for h in check_alert(session, alert, stats))
        session.commit()
    assert seen == set(names)
    assert stats.files_waiting > 0


# --- delivery and retries (#328) ----------------------------------------------


class _FakeHttpxClient:
    def __init__(self, fake_post, **_kwargs):
        self._fake_post = fake_post

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def post(self, url, *, json, headers=None, extensions=None):
        return self._fake_post(url, json=json, headers=headers, extensions=extensions)


def _posts(monkeypatch, *, fail=False):
    import httpx

    posted = []

    def fake_post(url, json, headers, extensions):
        if fail:
            raise httpx.ConnectError("connection refused", request=None)
        posted.append({"url": url, "json": json, "headers": headers})
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("app.alerts.httpx.Client", lambda **kw: _FakeHttpxClient(fake_post, **kw))
    return posted


def test_post_webhook_pins_the_validated_address(monkeypatch):
    posted = _posts(monkeypatch)
    assert post_webhook("https://example.com/hook", {"x": 1}) is None
    assert posted[0]["url"] == "https://93.184.216.34:443/hook"
    assert posted[0]["headers"] == {"Host": "example.com"}


def test_post_webhook_blocks_a_url_that_now_resolves_privately(monkeypatch):
    import socket

    posted = _posts(monkeypatch)
    monkeypatch.setattr(
        "app.webhook_safety.socket.getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    assert post_webhook("https://rebinds.example/hook", {}).startswith("blocked")
    assert posted == []


def test_a_failed_delivery_is_retried_then_given_up_after_an_hour(session, monkeypatch, tmp_path):
    user = _user(session)
    alert = _alert(session, user)
    hit = alerts.Hit(1, "src", "app.log", 0, "refused")
    _posts(monkeypatch, fail=True)
    queue_delivery(session, alert, [hit])
    session.commit()

    deliver_due(session)
    delivery = session.exec(select(AlertDelivery)).one()
    assert delivery.attempts == 1 and delivery.next_attempt_at > utcnow()
    assert alert.failed_deliveries == 0 and "refused" in alert.last_error

    delivery.created_at = utcnow() - timedelta(hours=2)
    delivery.next_attempt_at = utcnow()
    session.add(delivery)
    session.commit()
    deliver_due(session)
    session.refresh(alert)
    assert session.exec(select(AlertDelivery)).all() == []
    assert alert.failed_deliveries == 1
    # Only the delivery given up is audited, not each retry before it.
    from app.auth.models import AuditLog

    [entry] = session.exec(select(AuditLog).where(AuditLog.action == "alert.delivery_failed")).all()
    assert (entry.target_type, entry.target_id) == ("alert", alert.id)
    assert entry.event_metadata["attempts"] == 2
    assert "refused" in entry.event_metadata["error"]


def test_a_retry_that_works_clears_the_error(session, monkeypatch):
    user = _user(session)
    alert = _alert(session, user, last_error="earlier failure")
    posted = _posts(monkeypatch)
    queue_delivery(session, alert, [alerts.Hit(1, "src", "app.log", 0, "refused")])
    session.commit()
    deliver_due(session)
    session.refresh(alert)
    assert len(posted) == 1 and posted[0]["json"]["hits"][0]["line"] == "refused"
    assert alert.last_error is None and alert.last_fired_at is not None


def test_a_pass_reads_sends_and_records(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    posted = _posts(monkeypatch)
    with Session(engine) as session:
        user = _user(session)
        source = _source(session, tmp_path)
        _write(tmp_path, "app.log", "")
        _index(session, source, tmp_path, "app.log")
        alert = _alert(session, user)
        disabled = _alert(session, user, enabled=False)
        alert_id, disabled_id = alert.id, disabled.id

    run_alert_pass(engine, force=True)
    _write(tmp_path, "app.log", "connection refused\n", mode="a")
    stats = run_alert_pass(engine, force=True)
    assert stats.hits == 1 and stats.alerts == 1
    assert [p["json"]["alert_id"] for p in posted] == [alert_id]
    with Session(engine) as session:
        assert session.get(Alert, alert_id).last_fired_at is not None
        assert session.get(Alert, disabled_id).last_checked_at is None
    # Not due again until the interval has gone by.
    assert run_alert_pass(engine) is None


def test_only_files_that_changed_size_are_read(session, tmp_path, monkeypatch):
    from app.collectors import local

    user = _user(session)
    source = _source(session, tmp_path)
    for name in ("quiet.log", "busy.log"):
        _write(tmp_path, name, "")
    _index(session, source, tmp_path, "quiet.log", "busy.log")
    alert = _alert(session, user)
    check_alert(session, alert)
    session.commit()
    _write(tmp_path, "busy.log", "refused\n", mode="a")

    reads = []
    original = local.read_range
    monkeypatch.setattr(
        local,
        "read_range",
        lambda src, path, *a, **k: reads.append(path) or original(src, path, *a, **k),
    )
    assert _lines(check_alert(session, alert)) == ["refused"]
    assert reads == ["busy.log"]


def test_a_queued_delivery_is_dropped_once_the_owner_may_not_alert(session, monkeypatch):
    user = _user(session)
    alert = _alert(session, user)
    posted = _posts(monkeypatch)
    queue_delivery(session, alert, [alerts.Hit(1, "src", "app.log", 0, "refused")])
    user.active = False
    session.add(user)
    session.commit()
    deliver_due(session)
    assert posted == [] and session.exec(select(AlertDelivery)).all() == []


def test_a_webhook_error_never_carries_the_url(monkeypatch):
    import httpx

    def fake_post(url, json, headers, extensions):
        request = httpx.Request("POST", url)
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("boom " + url, request=request, response=response)

    monkeypatch.setattr("app.alerts.httpx.Client", lambda **kw: _FakeHttpxClient(fake_post, **kw))
    assert post_webhook("https://example.com/hook?token=s3cret", {}) == "HTTP 500"


# --- redaction (#382, #249) ----------------------------------------------------
#
# When redaction applies to a source, the query runs on the redacted lines
# (a replaced value can't be matched) and only redacted lines are queued and
# sent; offsets stay those of the original.


def _line_starts(content: bytes, newline: bytes = b"\n") -> dict[int, bytes]:
    """{offset: line} for each whole line of `content` (a UTF-16 newline
    only on an even offset)."""
    starts: dict[int, bytes] = {}
    start = 0
    index = content.find(newline)
    while index >= 0:
        if index % len(newline):
            index = content.find(newline, index + 1)
            continue
        starts[start] = content[start:index]
        start = index + len(newline)
        index = content.find(newline, start)
    return starts


def _redacting(session) -> None:
    from tests.test_redaction_endpoints import _enable_redaction

    _enable_redaction(session)


def _started(session, tmp_path, initial: str | bytes = "", **alert_fields):
    """(source, alert) on a local source's app.log holding `initial`, after
    the alert's first pass (which starts at the end)."""
    user = _user(session)
    source = _source(session, tmp_path)
    _write_bytes(tmp_path, "app.log", initial)
    _index(session, source, tmp_path, "app.log")
    alert = _alert(session, user, **alert_fields)
    assert check_alert(session, alert) == []
    session.commit()
    return source, alert


def _write_bytes(base, rel, data: str | bytes, mode="wb"):
    with open(base / rel, mode) as f:
        f.write(data.encode() if isinstance(data, str) else data)


def _offset(session, alert) -> int:
    session.expire_all()
    query = select(AlertFileState).where(AlertFileState.alert_id == alert.id)
    return session.exec(query).one().offset


def test_with_redaction_alerts_match_and_send_only_redacted_lines(session, tmp_path):
    from tests.test_redaction_endpoints import SAMPLE, assert_no_secret

    _redacting(session)
    source, alert = _started(session, tmp_path, "start\n", query=".", regex=True)
    _write_bytes(tmp_path, "app.log", SAMPLE, mode="ab")
    hits = check_alert(session, alert)
    session.commit()
    content = ("start\n" + SAMPLE).encode()
    starts = _line_starts(content)
    # Each line where it is in the original (the private key's body and END
    # lines are left empty, so they don't match).
    assert {h.offset for h in hits} <= set(starts)
    assert [h.offset for h in hits][:6] == sorted(starts)[1:7]
    for hit in hits:
        assert_no_secret(hit.line)
    for hit in hits[:5]:
        assert hit.line[:4] == starts[hit.offset][:4].decode()
    assert hits[5].line == "[redacted: private key]"
    queue_delivery(session, alert, hits)
    session.commit()
    delivery = session.exec(select(AlertDelivery)).one()
    assert_no_secret(json.dumps(delivery.payload))
    assert _offset(session, alert) == len(content)


@pytest.mark.parametrize("query", ["hunter2", "alice@example.com", "Sup3rS3cret", "4111 1111"])
def test_a_redacted_value_is_never_matched(session, tmp_path, query):
    from tests.test_redaction_endpoints import SAMPLE

    _redacting(session)
    _, alert = _started(session, tmp_path, query=query)
    _write_bytes(tmp_path, "app.log", SAMPLE, mode="ab")
    assert check_alert(session, alert) == []


def test_the_key_of_a_value_that_was_replaced_still_matches(session, tmp_path):
    _redacting(session)
    _, alert = _started(session, tmp_path, query="password=")
    _write_bytes(tmp_path, "app.log", "login password=hunter2 ok\n", mode="ab")
    hits = check_alert(session, alert)
    assert len(hits) == 1 and "hunter2" not in hits[0].line and hits[0].offset == 0


def test_a_private_key_begun_before_the_offset_stays_redacted(session, tmp_path):
    # The BEGIN was read on an earlier pass: the context read before the
    # offset tells the next one it is still inside the key.
    _redacting(session)
    _, alert = _started(
        session, tmp_path, "ok\n-----BEGIN RSA PRIVATE KEY-----\n", query=".", regex=True
    )
    _write_bytes(
        tmp_path,
        "app.log",
        "MIIEsecretkeybody\n-----END RSA PRIVATE KEY-----\nafter the key\n",
        mode="ab",
    )
    hits = check_alert(session, alert)
    assert [h.line for h in hits] == ["after the key"]


def test_an_offset_inside_a_line_starts_at_the_next(session, tmp_path):
    # A file first seen while a line was being written starts at its end,
    # inside that line: what follows of it is never matched in part.
    _redacting(session)
    _, alert = _started(session, tmp_path, "half a li", query=".", regex=True)
    _write_bytes(tmp_path, "app.log", "ne alice@example.com\nnext line\n", mode="ab")
    hits = check_alert(session, alert)
    expected = len("half a line alice@example.com\n")
    assert [(h.offset, h.line) for h in hits] == [(expected, "next line")]


def test_an_unfinished_line_waits_for_its_newline(session, tmp_path):
    _redacting(session)
    _, alert = _started(session, tmp_path, query="card")
    _write_bytes(tmp_path, "app.log", "card 4111 1111", mode="ab")
    assert check_alert(session, alert) == [] and _offset(session, alert) == 0
    _write_bytes(tmp_path, "app.log", " 1111 1111 paid\n", mode="ab")
    hits = check_alert(session, alert)
    assert len(hits) == 1 and hits[0].offset == 0 and "4111" not in hits[0].line


def test_utf16_lines_are_redacted_with_their_original_offsets(session, tmp_path):
    _redacting(session)
    _, alert = _started(session, tmp_path, "﻿start\n".encode("utf-16-le"), query="user")
    _write_bytes(
        tmp_path, "app.log", "x\nuser alice@example.com in\n".encode("utf-16-le"), mode="ab"
    )
    hits = check_alert(session, alert)
    assert [(h.offset, h.line) for h in hits] == [(len("﻿start\nx\n") * 2, hits[0].line)]
    assert hits[0].line.startswith("user ") and "alice@" not in hits[0].line


def test_a_line_with_no_end_within_a_read_becomes_a_marker(session, tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "READ_BYTES", 20_000)
    _redacting(session)
    _, alert = _started(session, tmp_path, query=".", regex=True)
    _write_bytes(tmp_path, "app.log", "x" * 50_000 + " alice@example.com\nok\n", mode="ab")
    lines = []
    for _ in range(20):
        lines += [(h.offset, h.line) for h in check_alert(session, alert)]
        session.commit()
    assert lines[0] == (0, "[redacted: line over 20000 bytes]")
    assert lines[-1] == (50_000 + len(" alice@example.com\n"), "ok")
    assert all("alice@" not in line for _, line in lines)


def test_compressed_content_is_never_matched(session, tmp_path):
    from tests.test_redaction_endpoints import SAMPLE, compressed_sample

    _redacting(session)
    _, alert = _started(session, tmp_path, query=".", regex=True)
    _write_bytes(tmp_path, "app.log", compressed_sample("gzip", SAMPLE.encode()), mode="ab")
    assert check_alert(session, alert) == [] and _offset(session, alert) == 0


def test_rules_that_cant_run_leave_the_source_waiting(session, tmp_path, monkeypatch):
    from app.redaction import RedactionRuleError

    _redacting(session)
    _, alert = _started(session, tmp_path, query="refused")

    def broken(rules):
        raise RedactionRuleError("can't compile")

    monkeypatch.setattr(alerts, "compile_rules", broken)
    _write_bytes(tmp_path, "app.log", "refused\n", mode="ab")
    assert check_alert(session, alert) == [] and _offset(session, alert) == 0


def test_turning_redaction_on_or_off_neither_restarts_nor_repeats(session, tmp_path):
    from app import system_settings

    _redacting(session)
    system_settings.set_bool(session, "redaction_enabled", False)
    session.commit()
    _, alert = _started(session, tmp_path, "old refused\n", query="refused")
    seen = []
    for step, enabled in enumerate((False, True, True, False, True)):
        system_settings.set_bool(session, "redaction_enabled", enabled)
        session.commit()
        _write_bytes(tmp_path, "app.log", f"refused {step} user=a@b.cd\n", mode="ab")
        seen += [h.line.split(" user=")[0] for h in check_alert(session, alert)]
        session.commit()
        assert _offset(session, alert) == (tmp_path / "app.log").stat().st_size
    assert seen == [f"refused {step}" for step in range(5)]


def test_the_context_is_the_tails():
    from app.api.archive import FOLLOW_CONTEXT_BYTES

    assert alerts.CONTEXT_BYTES == FOLLOW_CONTEXT_BYTES


def test_a_delivery_queued_before_redaction_was_on_goes_out_redacted(
    session, tmp_path, monkeypatch
):
    from tests.test_redaction_endpoints import SAMPLE, assert_no_secret

    _, alert = _started(session, tmp_path, query=".", regex=True)
    _write_bytes(tmp_path, "app.log", SAMPLE, mode="ab")
    queue_delivery(session, alert, check_alert(session, alert))
    session.commit()
    # Matched and queued while redaction was off: the values are in it.
    assert "hunter2" in json.dumps(session.exec(select(AlertDelivery)).one().payload)

    _redacting(session)
    posted = _posts(monkeypatch)
    deliver_due(session)
    [sent] = [p["json"] for p in posted]
    assert_no_secret(json.dumps(sent))
    assert [hit["offset"] for hit in sent["hits"]] == sorted(_line_starts(SAMPLE.encode()))
    # The key's body, after its BEGIN line, is redacted with it.
    assert sent["hits"][-3]["line"] == "[redacted: private key]"
    assert sent["hits"][-2]["line"] == ""
    assert "password=" in sent["hits"][3]["line"]


def test_a_delivery_matched_redacted_goes_out_the_same(session, tmp_path, monkeypatch):
    _redacting(session)
    _, alert = _started(session, tmp_path, query="password=")
    _write_bytes(tmp_path, "app.log", "login password=hunter2 ok\n", mode="ab")
    queue_delivery(session, alert, check_alert(session, alert))
    session.commit()
    queued = session.exec(select(AlertDelivery)).one().payload
    posted = _posts(monkeypatch)
    deliver_due(session)
    assert [p["json"] for p in posted] == [queued]


def test_a_delivery_isnt_sent_while_the_rules_cant_run(session, tmp_path, monkeypatch):
    from app.redaction import RedactionRuleError

    _, alert = _started(session, tmp_path, query="password=")
    _write_bytes(tmp_path, "app.log", "login password=hunter2 ok\n", mode="ab")
    queue_delivery(session, alert, check_alert(session, alert))
    session.commit()
    _redacting(session)

    def broken(rules):
        raise RedactionRuleError("can't compile")

    monkeypatch.setattr(alerts, "compile_rules", broken)
    posted = _posts(monkeypatch)
    deliver_due(session)
    session.refresh(alert)
    assert posted == []
    delivery = session.exec(select(AlertDelivery)).one()
    assert delivery.attempts == 1 and alert.last_error == "redaction failed"


def _read_failures(logs) -> int:
    return sum(1 for entry in logs if entry["event"] == "alert.file_read_failed")


def test_a_file_redaction_cant_read_is_logged_once_until_it_changes(session, tmp_path):
    import os

    from structlog.testing import capture_logs

    from tests.test_redaction_endpoints import SAMPLE, compressed_sample

    _redacting(session)
    _, alert = _started(session, tmp_path, query=".", regex=True)
    _write_bytes(tmp_path, "app.log", compressed_sample("gzip", SAMPLE.encode()), mode="ab")
    path = tmp_path / "app.log"
    with capture_logs() as logs:
        for _ in range(3):
            assert check_alert(session, alert) == []
            session.commit()
    assert _read_failures(logs) == 1 and _offset(session, alert) == 0

    # It grew, then only its modification time changed: logged each time.
    _write_bytes(tmp_path, "app.log", b"more\n", mode="ab")
    with capture_logs() as logs:
        check_alert(session, alert)
        check_alert(session, alert)
    assert _read_failures(logs) == 1
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 60))
    with capture_logs() as logs:
        check_alert(session, alert)
        check_alert(session, alert)
    assert _read_failures(logs) == 1

    # Its start changed, at the same size and time: logged again.
    data = bytearray(path.read_bytes())
    data[10] ^= 0xFF
    stat = path.stat()
    path.write_bytes(bytes(data))
    os.utime(path, (stat.st_atime, stat.st_mtime))
    with capture_logs() as logs:
        check_alert(session, alert)
        check_alert(session, alert)
    assert _read_failures(logs) == 1


def test_an_error_that_isnt_redaction_is_logged_every_time(session, tmp_path, monkeypatch):
    from app.collectors import local
    from structlog.testing import capture_logs

    _redacting(session)
    _, alert = _started(session, tmp_path, query="refused")
    _write_bytes(tmp_path, "app.log", "refused\n", mode="ab")

    def unreachable(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(local, "read_range", unreachable)
    with capture_logs() as logs:
        check_alert(session, alert)
        check_alert(session, alert)
    assert _read_failures(logs) == 2


@pytest.mark.parametrize(
    "change",
    [
        lambda session: alerts.forget_alert(session, 1),
        lambda session: alerts.forget_source(session, 2),
        lambda session: alerts._note_unreadable((1, 2, "other.log"), (5, None)),
        lambda session: alerts._clear_unreadable((1, 2, "app.log")),
    ],
    ids=["forget_alert", "forget_source", "the pass noting", "the pass clearing"],
)
def test_unreadable_files_are_changed_under_one_lock(session, change):
    # The pass writes them from the scheduler's thread while a request
    # deletes an alert or a source: each waits for the other.
    import threading

    alerts._unreadable[(1, 2, "app.log")] = (5, None)
    worker = threading.Thread(target=change, args=(session,))
    with alerts._unreadable_lock:
        worker.start()
        worker.join(timeout=0.3)
        assert worker.is_alive()
        assert list(alerts._unreadable) == [(1, 2, "app.log")]
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_forgetting_drops_only_the_alerts_or_sources_unreadable_files(session):
    alerts._unreadable.update({(1, 2, "a.log"): (1, None), (3, 4, "b.log"): (1, None)})
    alerts.forget_alert(session, 1)
    assert list(alerts._unreadable) == [(3, 4, "b.log")]
    alerts.forget_source(session, 4)
    assert alerts._unreadable == {}


# On an agent that redacts on its host the lines arrive redacted, with where
# each one is in the original; the original never reaches the server.


@pytest.fixture()
def redacting_agent(session, monkeypatch):
    """(source, agent, alert): an agent source with redaction on, whose
    agent redacts on its host and also answers plain reads (when redaction
    is off), with an alert matching every line after its first pass on an
    app.log holding "start\\n"."""
    from app.agent_registry import get_agent_registry

    from tests.test_redaction_agent import FakeAgent

    class Agent(FakeAgent):
        def __call__(self, source_id, command_type, *args, **fields):
            if command_type == "read_range" and "redact" not in fields:
                self.commands.append((command_type, fields))
                content = self.files[fields["path"]]
                offset, length = fields["offset"], fields["length"]
                return {
                    "size": len(content),
                    "head_b64": base64.b64encode(content[: fields["head_length"]]).decode(),
                    "data_b64": base64.b64encode(content[offset : offset + length]).decode(),
                }
            return super().__call__(source_id, command_type, *args, **fields)

    _redacting(session)
    user = _user(session)
    source = Source(name="agent01", protocol=Protocol.agent, host="agent01", base_path="/var/log")
    session.add(source)
    session.commit()
    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**",
            pattern_kind=PatternKind.glob,
        )
    )
    agent = Agent({"app.log": b"start\n"})
    registry = get_agent_registry()
    monkeypatch.setattr(registry, "send_command_sync", agent)
    monkeypatch.setattr(registry, "redacts_on_host", lambda source_id: True)
    session.add(FileNameEntry(source_id=source.id, path="app.log", size=6, seen_at=utcnow()))
    session.commit()
    alert = _alert(session, user, query=".", regex=True)
    assert check_alert(session, alert) == []
    session.commit()
    return source, agent, alert


def test_on_the_agent_lines_are_redacted_with_their_original_offsets(session, redacting_agent):
    from tests.test_redaction_endpoints import SAMPLE, assert_no_secret

    _, agent, alert = redacting_agent
    agent.files["app.log"] += b"x\n" + SAMPLE.encode() + b"unfinished 4111"
    hits = check_alert(session, alert)
    session.commit()
    starts = _line_starts(agent.files["app.log"])
    assert {h.offset for h in hits} <= set(starts)
    assert [h.offset for h in hits][:7] == sorted(starts)[1:8]
    for hit in hits:
        assert_no_secret(hit.line)
    for hit in hits[:6]:
        assert hit.line[:1] == starts[hit.offset][:1].decode()
    reads = [fields for kind, fields in agent.commands if kind == "read_range"]
    assert reads and all("redact" in f and f["with_line_offsets"] is True for f in reads)
    # The unfinished line waits for its newline.
    assert _offset(session, alert) == len(agent.files["app.log"]) - len(b"unfinished 4111")


def test_on_the_agent_utf16_and_an_offset_inside_a_line(session, redacting_agent):
    _, agent, alert = redacting_agent
    content = "﻿start\nhalf".encode("utf-16-le")
    agent.files["app.log"] = content
    state = session.exec(select(AlertFileState)).one()
    state.offset, state.head = len(content), None
    session.add(state)
    session.commit()
    agent.files["app.log"] += " a line alice@example.com\nuser bob@example.com in\n".encode(
        "utf-16-le"
    )
    hits = check_alert(session, alert)
    expected = len("﻿start\nhalf a line alice@example.com\n".encode("utf-16-le"))
    assert [h.offset for h in hits] == [expected]
    assert hits[0].line.startswith("user ") and "bob@" not in hits[0].line


@pytest.mark.parametrize(
    "tamper",
    [
        {"changed_lines": None},  # an agent that doesn't say where its lines are
        {"changed_lines": [[0, 10_000]]},  # lines that would start before the offset
        {"changed_lines": [[5, 3]]},  # a line that isn't there
        {"changed_lines": [[0, 3], [0, 4]]},  # out of order
    ],
)
def test_on_the_agent_an_answer_without_good_line_offsets_isnt_used(
    session, redacting_agent, tamper
):
    _, agent, alert = redacting_agent
    agent.files["app.log"] += b"refused password=hunter2\n"
    agent.tamper = tamper
    assert check_alert(session, alert) == [] and _offset(session, alert) == 6


def test_on_the_agent_turning_redaction_on_or_off_neither_restarts_nor_repeats(
    session, redacting_agent
):
    from app import system_settings

    _, agent, alert = redacting_agent
    seen = []
    for step, enabled in enumerate((True, False, True, False)):
        system_settings.set_bool(session, "redaction_enabled", enabled)
        session.commit()
        agent.files["app.log"] += f"line {step} user=a@b.cd\n".encode()
        seen += [h.line.split(" user=")[0] for h in check_alert(session, alert)]
        session.commit()
        assert _offset(session, alert) == len(agent.files["app.log"])
    assert seen == [f"line {step}" for step in range(4)]
    redacted = [f for kind, f in agent.commands if kind == "read_range" and "redact" in f]
    plain = [f for kind, f in agent.commands if kind == "read_range" and "redact" not in f]
    assert len(redacted) == 2 and len(plain) == 2


def test_on_the_agent_a_file_it_cant_redact_is_logged_once_until_it_changes(
    session, redacting_agent
):
    from structlog.testing import capture_logs

    from tests.test_redaction_agent import _refusal

    _, agent, alert = redacting_agent
    agent.files["app.log"] += b"refused\n"
    agent.refuse = _refusal("unsupported_encoding")
    with capture_logs() as logs:
        for _ in range(3):
            assert check_alert(session, alert) == []
            session.commit()
    assert _read_failures(logs) == 1 and _offset(session, alert) == 6
    agent.files["app.log"] += b"more\n"
    with capture_logs() as logs:
        check_alert(session, alert)
        check_alert(session, alert)
    assert _read_failures(logs) == 1


def test_an_agent_that_cant_redact_falls_back_to_the_server(session, redacting_agent, monkeypatch):
    from app.agent_registry import get_agent_registry

    _, agent, alert = redacting_agent
    monkeypatch.setattr(get_agent_registry(), "redacts_on_host", lambda source_id: False)
    agent.files["app.log"] += b"login password=hunter2 ok\n"
    hits = check_alert(session, alert)
    assert len(hits) == 1 and hits[0].offset == 6 and "hunter2" not in hits[0].line
    assert all("redact" not in f for kind, f in agent.commands if kind == "read_range")
