import pytest
from app.api.auth import get_current_active_user
from app.api.rules import router as rules_router
from app.auth.models import Capability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Customer, Protocol, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_role_and_user(session, *, is_super_admin=False) -> tuple[Role, User]:
    role = Role(name=f"role-{is_super_admin}-{id(object())}", is_super_admin=is_super_admin)
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"user-{role.id}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return role, user


def _make_source(session, *, is_system=False) -> Source:
    customer = Customer(name="Vodacom Tanzania")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = Source(
        name="app01",
        customer_id=None if is_system else customer.id,
        protocol=Protocol.local if is_system else Protocol.ssh,
        host="app01",
        base_path="/var/log/appname",
        is_system=is_system,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _grant(session, role, source, capabilities):
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=capabilities,
        )
    )
    session.commit()


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(rules_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def test_view_grant_can_list_but_not_create(session, client_for):
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.view])
    client = client_for(user)

    assert client.get(f"/sources/{source.id}/rules").status_code == 200
    create = client.post(
        f"/sources/{source.id}/rules", json={"type": "include", "pattern": "**/*.log"}
    )
    assert create.status_code == 403


def test_manage_rules_grant_full_crud(session, client_for):
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.manage_rules, Capability.view])
    client = client_for(user)

    created = client.post(
        f"/sources/{source.id}/rules", json={"type": "include", "pattern": "**/*.log"}
    )
    assert created.status_code == 201
    rule_id = created.json()["id"]
    assert created.json()["order"] == 0
    assert created.json()["pattern_kind"] == "glob"

    updated = client.patch(
        f"/sources/{source.id}/rules/{rule_id}", json={"pattern": "re:access-\\d+\\.log"}
    )
    assert updated.status_code == 200
    assert updated.json()["pattern_kind"] == "regex"
    assert updated.json()["pattern"] == "access-\\d+\\.log"

    deleted = client.delete(f"/sources/{source.id}/rules/{rule_id}")
    assert deleted.status_code == 204
    assert client.get(f"/sources/{source.id}/rules").json() == []


def test_second_created_rule_appends_after_first(session, client_for):
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.manage_rules])
    client = client_for(user)

    client.post(f"/sources/{source.id}/rules", json={"type": "include", "pattern": "a"})
    second = client.post(f"/sources/{source.id}/rules", json={"type": "exclude", "pattern": "b"})
    assert second.json()["order"] == 1


def test_reorder_rules(session, client_for):
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.manage_rules])
    client = client_for(user)

    first = client.post(
        f"/sources/{source.id}/rules", json={"type": "include", "pattern": "a"}
    ).json()
    second = client.post(
        f"/sources/{source.id}/rules", json={"type": "include", "pattern": "b"}
    ).json()

    response = client.post(
        f"/sources/{source.id}/rules/reorder",
        json={"rule_ids": [second["id"], first["id"]]},
    )
    assert response.status_code == 200
    ordered = response.json()
    assert [r["id"] for r in ordered] == [second["id"], first["id"]]
    assert ordered[0]["order"] == 0
    assert ordered[1]["order"] == 1


def test_reorder_rejects_incomplete_id_set(session, client_for):
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.manage_rules])
    client = client_for(user)

    first = client.post(
        f"/sources/{source.id}/rules", json={"type": "include", "pattern": "a"}
    ).json()
    client.post(f"/sources/{source.id}/rules", json={"type": "include", "pattern": "b"})

    response = client.post(f"/sources/{source.id}/rules/reorder", json={"rule_ids": [first["id"]]})
    assert response.status_code == 400


def test_raw_paste_mode_replaces_all_rules(session, client_for):
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.manage_rules])
    client = client_for(user)

    client.post(f"/sources/{source.id}/rules", json={"type": "include", "pattern": "stale"})

    raw = "\n".join(
        [
            "# comment, ignored",
            "",
            "**/*.log",
            "!**/*.debug.log",
            "re:^access-\\d+\\.log$",
        ]
    )
    response = client.put(f"/sources/{source.id}/rules/raw", json={"text": raw})
    assert response.status_code == 200
    rules = response.json()
    assert len(rules) == 3
    assert rules[0]["pattern"] == "**/*.log"
    assert rules[0]["type"] == "include"
    assert rules[1]["pattern"] == "**/*.debug.log"
    assert rules[1]["type"] == "exclude"
    assert rules[2]["pattern"] == "^access-\\d+\\.log$"
    assert rules[2]["type"] == "include"
    assert rules[2]["pattern_kind"] == "regex"


def test_system_source_rules_not_editable_even_for_super_admin(session, client_for):
    _, user = _make_role_and_user(session, is_super_admin=True)
    source = _make_source(session, is_system=True)
    client = client_for(user)

    response = client.post(
        f"/sources/{source.id}/rules", json={"type": "include", "pattern": "**/*"}
    )
    assert response.status_code == 403
