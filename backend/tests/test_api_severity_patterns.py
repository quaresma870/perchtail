import pytest
from app.api.auth import get_current_active_user
from app.api.severity_patterns import global_router, source_router
from app.auth.models import Capability, GlobalCapability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Customer, Protocol, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_role_and_user(
    session, *, is_super_admin=False, global_capabilities=None
) -> tuple[Role, User]:
    role = Role(
        name=f"role-{is_super_admin}-{global_capabilities}-{id(object())}",
        is_super_admin=is_super_admin,
        global_capabilities=global_capabilities or [],
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"user-{role.id}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return role, user


def _make_source(session) -> Source:
    customer = Customer(name=f"Vodacom Tanzania {id(object())}")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    source = Source(
        name="app01",
        customer_id=customer.id,
        protocol=Protocol.ssh,
        host="app01",
        base_path="/var/log",
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
        app.include_router(global_router)
        app.include_router(source_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


# --- Global set ---------------------------------------------------------


def test_plain_user_cannot_read_or_write_global_patterns(session, client_for):
    _, user = _make_role_and_user(session)
    client = client_for(user)

    assert client.get("/severity-patterns").status_code == 403
    assert (
        client.post(
            "/severity-patterns", json={"level": "error", "pattern": "re:panic"}
        ).status_code
        == 403
    )


def test_manage_system_settings_capability_can_crud_global_patterns(session, client_for):
    _, user = _make_role_and_user(
        session, global_capabilities=[GlobalCapability.manage_system_settings]
    )
    client = client_for(user)

    created = client.post("/severity-patterns", json={"level": "error", "pattern": "re:panic"})
    assert created.status_code == 201
    body = created.json()
    assert body["source_id"] is None
    assert body["pattern_kind"] == "regex"
    assert body["pattern"] == "panic"
    pattern_id = body["id"]

    listed = client.get("/severity-patterns")
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [pattern_id]

    updated = client.patch(f"/severity-patterns/{pattern_id}", json={"enabled": False})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False

    deleted = client.delete(f"/severity-patterns/{pattern_id}")
    assert deleted.status_code == 204
    assert client.get("/severity-patterns").json() == []


def test_super_admin_can_manage_global_patterns_without_the_explicit_capability(
    session, client_for
):
    _, user = _make_role_and_user(session, is_super_admin=True)
    client = client_for(user)

    created = client.post("/severity-patterns", json={"level": "info", "pattern": "startup"})
    assert created.status_code == 201


# --- Per-source overrides ------------------------------------------------


def test_view_grant_can_read_effective_but_not_manage_overrides(session, client_for):
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.view])
    client = client_for(user)

    assert client.get(f"/sources/{source.id}/severity-patterns/effective").status_code == 200
    assert client.get(f"/sources/{source.id}/severity-patterns").status_code == 403
    create = client.post(
        f"/sources/{source.id}/severity-patterns", json={"level": "warning", "pattern": "retry"}
    )
    assert create.status_code == 403


def test_manage_rules_grant_full_crud_on_source_overrides(session, client_for):
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.manage_rules, Capability.view])
    client = client_for(user)

    created = client.post(
        f"/sources/{source.id}/severity-patterns", json={"level": "warning", "pattern": "retry"}
    )
    assert created.status_code == 201
    pattern_id = created.json()["id"]
    assert created.json()["source_id"] == source.id

    listed = client.get(f"/sources/{source.id}/severity-patterns")
    assert [p["id"] for p in listed.json()] == [pattern_id]

    updated = client.patch(
        f"/sources/{source.id}/severity-patterns/{pattern_id}", json={"pattern": "re:retry\\d+"}
    )
    assert updated.status_code == 200
    assert updated.json()["pattern_kind"] == "regex"

    deleted = client.delete(f"/sources/{source.id}/severity-patterns/{pattern_id}")
    assert deleted.status_code == 204


def test_effective_endpoint_prefers_source_overrides_over_global(session, client_for):
    _, admin = _make_role_and_user(
        session, global_capabilities=[GlobalCapability.manage_system_settings]
    )
    role, user = _make_role_and_user(session)
    source = _make_source(session)
    _grant(session, role, source, [Capability.view, Capability.manage_rules])

    admin_client = client_for(admin)
    admin_client.post("/severity-patterns", json={"level": "error", "pattern": "re:panic"})

    viewer_client = client_for(user)
    effective = viewer_client.get(f"/sources/{source.id}/severity-patterns/effective")
    assert effective.status_code == 200
    assert len(effective.json()) == 1
    assert effective.json()[0]["source_id"] is None

    viewer_client.post(
        f"/sources/{source.id}/severity-patterns", json={"level": "warning", "pattern": "retry"}
    )
    effective_after_override = viewer_client.get(
        f"/sources/{source.id}/severity-patterns/effective"
    )
    assert len(effective_after_override.json()) == 1
    assert effective_after_override.json()[0]["source_id"] == source.id


def test_cannot_patch_another_sources_pattern(session, client_for):
    role, user = _make_role_and_user(session)
    source_a = _make_source(session)
    source_b = _make_source(session)
    _grant(session, role, source_a, [Capability.manage_rules, Capability.view])
    _grant(session, role, source_b, [Capability.manage_rules, Capability.view])
    client = client_for(user)

    created = client.post(
        f"/sources/{source_a.id}/severity-patterns", json={"level": "warning", "pattern": "retry"}
    )
    pattern_id = created.json()["id"]

    cross = client.patch(
        f"/sources/{source_b.id}/severity-patterns/{pattern_id}", json={"enabled": False}
    )
    assert cross.status_code == 404
