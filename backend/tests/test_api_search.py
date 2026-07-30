import pytest
from app.api.auth import get_current_active_user
from app.api.search import router as search_router
from app.auth.models import Capability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from app.search_index import index_source
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_user(session, *, is_super_admin=False) -> User:
    role = Role(name=f"role-{is_super_admin}-{id(object())}", is_super_admin=is_super_admin)
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"user-{role.id}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _source(session, tmp_path, **overrides) -> Source:
    defaults = dict(
        name="app01",
        protocol=Protocol.local,
        host="localhost",
        base_path=str(tmp_path),
        search_indexing_enabled=True,
    )
    defaults.update(overrides)
    source = Source(**defaults)
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _rule(session, source_id, pattern) -> Rule:
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


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(search_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def test_search_returns_hits_for_visible_source(session, client_for, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    role = Role(name="viewer")
    session.add(role)
    session.commit()
    session.refresh(role)
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=[Capability.view],
        )
    )
    session.commit()
    user = User(username="viewer@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)

    response = client_for(user).get("/search", params={"q": "refused"})
    assert response.status_code == 200
    hits = response.json()
    assert len(hits) == 1
    assert hits[0]["source_id"] == source.id
    assert hits[0]["file_path"] == "app.log"
    assert "<mark>refused</mark>" in hits[0]["snippet_html"]


def test_search_hides_hits_from_sources_without_a_grant(session, client_for, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    user = _make_user(session)  # no grants at all
    response = client_for(user).get("/search", params={"q": "refused"})
    assert response.status_code == 200
    assert response.json() == []


def test_search_super_admin_sees_all_sources(session, client_for, tmp_path):
    source = _source(session, tmp_path)
    _rule(session, source.id, "**/*.log")
    (tmp_path / "app.log").write_text("connection refused by upstream\n")
    index_source(session, source)

    admin = _make_user(session, is_super_admin=True)
    response = client_for(admin).get("/search", params={"q": "refused"})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_search_blank_query_returns_empty_without_error(session, client_for):
    admin = _make_user(session, is_super_admin=True)
    response = client_for(admin).get("/search", params={"q": "   "})
    assert response.status_code == 200
    assert response.json() == []


def test_search_missing_query_param_returns_empty(session, client_for):
    admin = _make_user(session, is_super_admin=True)
    response = client_for(admin).get("/search")
    assert response.status_code == 200
    assert response.json() == []
