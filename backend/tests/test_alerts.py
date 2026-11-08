"""Live alerts (#305) and their webhook delivery (#328)."""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app import alerts
from app.alerts import check_alert, deliver_due, post_webhook, queue_delivery, run_alert_pass
from app.auth.models import Capability, Role, ScopeType, User
from app.auth.rbac import create_role_grant
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
        webhook_url="https://example.com/hook",
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
