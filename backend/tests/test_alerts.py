from types import SimpleNamespace

from app.alerts import check_alert, evaluate_alerts, send_webhook
from app.auth.models import Capability, Role, ScopeType, User
from app.auth.rbac import create_role_grant
from app.db import ensure_search_schema
from app.models import Alert, PatternKind, Protocol, Rule, RuleType, Source
from app.search_index import SearchHit, index_source
from app.timeutils import utcnow
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


def _user(session, *, is_super_admin=False, active=True) -> User:
    role = Role(name=f"role-{id(object())}", is_super_admin=is_super_admin)
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"user-{id(object())}@example.com", role_id=role.id, active=active)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _source(session, tmp_path, *, name="app01", search_indexing_enabled=True) -> Source:
    source = Source(
        name=name,
        protocol=Protocol.local,
        host="localhost",
        base_path=str(tmp_path),
        search_indexing_enabled=search_indexing_enabled,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _rule(session, source_id, pattern="**/*.log"):
    rule = Rule(
        source_id=source_id,
        order=0,
        type=RuleType.include,
        pattern=pattern,
        pattern_kind=PatternKind.glob,
    )
    session.add(rule)
    session.commit()
    return rule


def _alert(session, user, **overrides) -> Alert:
    defaults = dict(
        user_id=user.id,
        name="my alert",
        query="refused",
        source_id=None,
        webhook_url="https://example.com/hook",
        enabled=True,
        created_at=utcnow(),
    )
    defaults.update(overrides)
    alert = Alert(**defaults)
    session.add(alert)
    session.commit()
    session.refresh(alert)
    return alert


def test_check_alert_finds_hits_on_first_evaluation(session, tmp_path):
    user = _user(session, is_super_admin=True)
    source = _source(session, tmp_path)
    _rule(session, source.id)
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    alert = _alert(session, user, query="refused")

    hits = check_alert(session, alert)

    assert len(hits) == 1
    assert hits[0].file_path == "app.log"


def test_check_alert_returns_nothing_once_caught_up(session, tmp_path):
    user = _user(session, is_super_admin=True)
    source = _source(session, tmp_path)
    _rule(session, source.id)
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    alert = _alert(session, user, query="refused", last_checked_at=utcnow())

    assert check_alert(session, alert) == []


def test_check_alert_finds_only_content_added_after_last_check(session, tmp_path):
    user = _user(session, is_super_admin=True)
    source = _source(session, tmp_path)
    _rule(session, source.id)
    log_path = tmp_path / "app.log"
    log_path.write_text("nothing interesting yet\n")
    index_source(session, source)

    alert = _alert(session, user, query="refused", last_checked_at=utcnow())
    assert check_alert(session, alert) == []

    log_path.write_text("nothing interesting yet\nconnection refused by upstream\n")
    index_source(session, source)

    hits = check_alert(session, alert)
    assert len(hits) == 1
    assert "refused" in hits[0].snippet_html


def test_check_alert_ignores_sources_not_opted_into_indexing(session, tmp_path):
    user = _user(session, is_super_admin=True)
    source = _source(session, tmp_path, search_indexing_enabled=False)
    _rule(session, source.id)
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    # Not indexed at all since the source never opted in.

    alert = _alert(session, user, query="refused")

    assert check_alert(session, alert) == []


def test_check_alert_respects_explicit_source_scope(session, tmp_path):
    user = _user(session, is_super_admin=True)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    source_a = _source(session, dir_a, name="source-a")
    source_b = _source(session, dir_b, name="source-b")
    _rule(session, source_a.id)
    _rule(session, source_b.id)
    (dir_a / "app.log").write_text("connection refused by upstream\n")
    (dir_b / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source_a)
    index_source(session, source_b)

    alert = _alert(session, user, query="refused", source_id=source_a.id)

    hits = check_alert(session, alert)
    assert {h.source_id for h in hits} == {source_a.id}


def test_check_alert_returns_nothing_when_owner_lacks_grant(session, tmp_path):
    owner = _user(session, is_super_admin=False)
    source = _source(session, tmp_path)
    _rule(session, source.id)
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    # No RoleGrant at all for owner's role -> resolve_capability denies.
    alert = _alert(session, owner, query="refused")

    assert check_alert(session, alert) == []


def test_check_alert_finds_hits_once_owner_is_granted_access(session, tmp_path):
    owner = _user(session, is_super_admin=False)
    source = _source(session, tmp_path)
    _rule(session, source.id)
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)
    from app.models import Customer

    customer = Customer(name="Acme")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source.customer_id = customer.id
    session.add(source)
    session.commit()

    create_role_grant(
        session,
        actor_user_id=None,
        role_id=owner.role_id,
        scope_type=ScopeType.customer,
        scope_id=customer.id,
        capabilities=[Capability.view],
    )

    alert = _alert(session, owner, query="refused")

    hits = check_alert(session, alert)
    assert len(hits) == 1


def test_check_alert_returns_nothing_for_inactive_owner(session, tmp_path):
    owner = _user(session, is_super_admin=True, active=False)
    source = _source(session, tmp_path)
    _rule(session, source.id)
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    alert = _alert(session, owner, query="refused")

    assert check_alert(session, alert) == []


def test_send_webhook_posts_payload_and_returns_true_on_success(monkeypatch):
    posted = {}

    def fake_post(url, json, timeout):
        posted["url"] = url
        posted["json"] = json
        posted["timeout"] = timeout
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("app.alerts.httpx.post", fake_post)

    alert = Alert(
        id=1,
        user_id=1,
        name="my alert",
        query="refused",
        webhook_url="https://example.com/hook",
        created_at=utcnow(),
    )
    hit = SearchHit(
        source_id=1, file_path="app.log", line_number=1, snippet_html="<mark>refused</mark>"
    )

    ok = send_webhook(alert, [hit])

    assert ok is True
    assert posted["url"] == "https://example.com/hook"
    assert posted["json"]["alert_id"] == 1
    assert posted["json"]["matched_count"] == 1
    assert posted["json"]["hits"][0]["file_path"] == "app.log"


def test_send_webhook_returns_false_on_http_error(monkeypatch):
    import httpx

    def fake_post(url, json, timeout):
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr("app.alerts.httpx.post", fake_post)

    alert = Alert(
        id=1,
        user_id=1,
        name="my alert",
        query="refused",
        webhook_url="https://example.com/hook",
        created_at=utcnow(),
    )

    assert send_webhook(alert, []) is False


def _throwaway_engine(monkeypatch):
    # evaluate_alerts() opens its own session against app.db.engine
    # (deferred import, same reason app.search_index.run_indexing_sweep
    # does) rather than accepting one as a parameter, so it needs
    # app.db.engine itself patched -- not just a local variable -- to see
    # data set up via a plain Session(engine) here. Same StaticPool/
    # in-memory setup conftest.py's `session` fixture uses.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    ensure_search_schema(engine)
    monkeypatch.setattr("app.db.engine", engine)
    return engine


def test_evaluate_alerts_sends_webhook_and_advances_last_checked_at(tmp_path, monkeypatch):
    engine = _throwaway_engine(monkeypatch)

    posted = []

    def fake_post(url, json, timeout):
        posted.append(json)
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("app.alerts.httpx.post", fake_post)

    with Session(engine) as session:
        user = _user(session, is_super_admin=True)
        source = _source(session, tmp_path)
        _rule(session, source.id)
        (tmp_path / "app.log").write_text("connection refused by upstream\n")
        index_source(session, source)

        alert = _alert(session, user, query="refused")
        assert alert.last_checked_at is None
        alert_id = alert.id

    evaluate_alerts()

    assert len(posted) == 1
    assert posted[0]["matched_count"] == 1
    with Session(engine) as session:
        assert session.get(Alert, alert_id).last_checked_at is not None


def test_evaluate_alerts_does_not_advance_watermark_when_fully_blocked(tmp_path, monkeypatch):
    engine = _throwaway_engine(monkeypatch)
    posted = []
    monkeypatch.setattr("app.alerts.httpx.post", lambda url, json, timeout: posted.append(json))

    with Session(engine) as session:
        owner = _user(session, is_super_admin=False)
        source = _source(session, tmp_path)
        _rule(session, source.id)
        (tmp_path / "app.log").write_text("connection refused by upstream\n")
        index_source(session, source)

        alert = _alert(session, owner, query="refused")
        alert_id = alert.id

    evaluate_alerts()

    assert posted == []
    with Session(engine) as session:
        assert session.get(Alert, alert_id).last_checked_at is None


def test_evaluate_alerts_skips_disabled_alerts(tmp_path, monkeypatch):
    engine = _throwaway_engine(monkeypatch)
    posted = []
    monkeypatch.setattr("app.alerts.httpx.post", lambda url, json, timeout: posted.append(json))

    with Session(engine) as session:
        user = _user(session, is_super_admin=True)
        source = _source(session, tmp_path)
        _rule(session, source.id)
        (tmp_path / "app.log").write_text("connection refused by upstream\n")
        index_source(session, source)

        _alert(session, user, query="refused", enabled=False)

    evaluate_alerts()

    assert posted == []
