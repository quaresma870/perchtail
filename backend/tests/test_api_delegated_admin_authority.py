import uuid

import pytest
from app.api.auth import get_current_active_user
from app.api.roles import router as roles_router
from app.api.sso import router as sso_router
from app.api.users import router as users_router
from app.auth.models import AuditLog, GlobalCapability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Customer
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


def _role(session, *, is_super_admin=False, caps=None, is_builtin=False) -> Role:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=is_super_admin,
        global_capabilities=caps or [],
        is_builtin=is_builtin,
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def _user(session, role: Role, *, active=True) -> User:
    user = User(username=f"user-{uuid.uuid4().hex}", role_id=role.id, active=active)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(users_router)
        app.include_router(roles_router)
        app.include_router(sso_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def _actions(session) -> list[str]:
    return [row.action for row in session.exec(select(AuditLog)).all()]


# --- users ---------------------------------------------------------------


def test_manage_users_cannot_assign_a_super_admin_role(session, client_for):
    super_role = _role(session, is_super_admin=True)
    actor = _user(session, _role(session, caps=[GlobalCapability.manage_users]))
    target = _user(session, _role(session))
    client = client_for(actor)

    assert client.patch(f"/users/{actor.id}", json={"role_id": super_role.id}).status_code == 403
    assert client.patch(f"/users/{target.id}", json={"role_id": super_role.id}).status_code == 403
    response = client.post(
        "/users", json={"username": "new@example.com", "password": "pw", "role_id": super_role.id}
    )
    assert response.status_code == 403


def test_manage_users_cannot_assign_a_role_with_capabilities_it_lacks(session, client_for):
    stronger = _role(session, caps=[GlobalCapability.manage_users, GlobalCapability.manage_roles])
    actor = _user(session, _role(session, caps=[GlobalCapability.manage_users]))
    target = _user(session, _role(session))

    response = client_for(actor).patch(f"/users/{target.id}", json={"role_id": stronger.id})
    assert response.status_code == 403


def test_manage_users_can_assign_an_equal_or_weaker_role(session, client_for):
    weaker = _role(session)
    peer = _role(session, caps=[GlobalCapability.manage_users])
    actor = _user(session, _role(session, caps=[GlobalCapability.manage_users]))
    target = _user(session, _role(session))
    client = client_for(actor)

    assert client.patch(f"/users/{target.id}", json={"role_id": weaker.id}).status_code == 200
    assert client.patch(f"/users/{target.id}", json={"role_id": peer.id}).status_code == 200


def test_manage_users_cannot_act_on_a_super_admin_account(session, client_for):
    super_role = _role(session, is_super_admin=True)
    victim = _user(session, super_role)
    _user(session, super_role)  # a second super-admin, so the last-one guard isn't what blocks
    actor = _user(session, _role(session, caps=[GlobalCapability.manage_users]))
    client = client_for(actor)

    assert client.post(f"/users/{victim.id}/reset-password").status_code == 403
    assert client.delete(f"/users/{victim.id}").status_code == 403
    assert client.patch(f"/users/{victim.id}", json={"active": False}).status_code == 403


def test_super_admin_can_still_manage_other_super_admins(session, client_for):
    super_role = _role(session, is_super_admin=True)
    actor = _user(session, super_role)
    other = _user(session, super_role)
    client = client_for(actor)

    assert client.post(f"/users/{other.id}/reset-password").status_code == 200
    assert client.delete(f"/users/{other.id}").status_code == 204


def test_last_active_super_admin_cannot_be_deactivated_or_demoted(session, client_for):
    super_role = _role(session, is_super_admin=True)
    actor = _user(session, super_role)
    _user(session, super_role, active=False)  # inactive ones don't count
    weaker = _role(session)
    client = client_for(actor)

    assert client.delete(f"/users/{actor.id}").status_code == 409
    assert client.patch(f"/users/{actor.id}", json={"active": False}).status_code == 409
    assert client.patch(f"/users/{actor.id}", json={"role_id": weaker.id}).status_code == 409


# --- roles ---------------------------------------------------------------


def test_manage_roles_cannot_grant_capabilities_it_lacks(session, client_for):
    actor = _user(session, _role(session, caps=[GlobalCapability.manage_roles]))
    client = client_for(actor)

    response = client.post(
        "/roles", json={"name": "escalated", "global_capabilities": ["manage_users"]}
    )
    assert response.status_code == 403

    own_role_id = actor.role_id
    response = client.patch(
        f"/roles/{own_role_id}", json={"global_capabilities": ["manage_roles", "manage_users"]}
    )
    assert response.status_code == 403


def test_manage_roles_cannot_touch_a_more_privileged_role(session, client_for):
    stronger = _role(session, caps=[GlobalCapability.manage_roles, GlobalCapability.manage_users])
    customer = Customer(name="c")
    session.add(customer)
    session.commit()
    actor = _user(session, _role(session, caps=[GlobalCapability.manage_roles]))
    client = client_for(actor)

    assert client.patch(f"/roles/{stronger.id}", json={"name": "x"}).status_code == 403
    assert client.delete(f"/roles/{stronger.id}").status_code == 403
    assert client.post(f"/roles/{stronger.id}/duplicate", json={}).status_code == 403
    response = client.post(
        f"/roles/{stronger.id}/grants",
        json={"scope_type": "customer", "scope_id": customer.id, "capabilities": ["view"]},
    )
    assert response.status_code == 403


def test_builtin_roles_are_immutable_even_for_super_admin(session, client_for):
    builtin = _role(session, is_builtin=True)
    customer = Customer(name="c")
    session.add(customer)
    session.commit()
    grant = RoleGrant(
        role_id=builtin.id, scope_type=ScopeType.customer, scope_id=customer.id, capabilities=[]
    )
    session.add(grant)
    session.commit()
    client = client_for(_user(session, _role(session, is_super_admin=True)))

    assert client.patch(f"/roles/{builtin.id}", json={"name": "renamed"}).status_code == 403
    assert (
        client.patch(f"/roles/{builtin.id}", json={"global_capabilities": ["manage_users"]})
    ).status_code == 403
    response = client.post(
        f"/roles/{builtin.id}/grants",
        json={"scope_type": "customer", "scope_id": customer.id, "capabilities": ["view"]},
    )
    assert response.status_code == 403
    response = client.patch(
        f"/roles/{builtin.id}/grants/{grant.id}", json={"capabilities": ["view"]}
    )
    assert response.status_code == 403
    assert client.delete(f"/roles/{builtin.id}/grants/{grant.id}").status_code == 403


def test_role_and_grant_changes_are_audited(session, client_for):
    customer = Customer(name="c")
    session.add(customer)
    session.commit()
    client = client_for(_user(session, _role(session, is_super_admin=True)))
    role = _role(session)

    assert client.patch(f"/roles/{role.id}", json={"name": "renamed"}).status_code == 200
    grant_id = client.post(
        f"/roles/{role.id}/grants",
        json={"scope_type": "customer", "scope_id": customer.id, "capabilities": ["view"]},
    ).json()["id"]
    assert (
        client.patch(f"/roles/{role.id}/grants/{grant_id}", json={"capabilities": ["download"]})
    ).status_code == 200
    assert client.delete(f"/roles/{role.id}/grants/{grant_id}").status_code == 204
    assert client.delete(f"/roles/{role.id}").status_code == 204

    actions = _actions(session)
    for expected in ("role.update", "role_grant.update", "role_grant.delete", "role.delete"):
        assert expected in actions


# --- SSO group mappings --------------------------------------------------


def test_manage_sso_cannot_map_a_group_to_a_more_privileged_role(session, client_for):
    super_role = _role(session, is_super_admin=True)
    stronger = _role(session, caps=[GlobalCapability.manage_users])
    weaker = _role(session)
    actor = _user(session, _role(session, caps=[GlobalCapability.manage_sso]))
    client = client_for(actor)

    for role in (super_role, stronger):
        response = client.post(
            "/sso/group-mappings", json={"order": 0, "group_name": "g", "role_id": role.id}
        )
        assert response.status_code == 403

    created = client.post(
        "/sso/group-mappings", json={"order": 0, "group_name": "g", "role_id": weaker.id}
    )
    assert created.status_code == 201
    mapping_id = created.json()["id"]
    response = client.patch(f"/sso/group-mappings/{mapping_id}", json={"role_id": super_role.id})
    assert response.status_code == 403


def test_super_admin_can_map_a_group_to_super_admin(session, client_for):
    super_role = _role(session, is_super_admin=True)
    client = client_for(_user(session, super_role))
    response = client.post(
        "/sso/group-mappings", json={"order": 0, "group_name": "admins", "role_id": super_role.id}
    )
    assert response.status_code == 201
