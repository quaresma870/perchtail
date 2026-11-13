"""A delegated admin (manage_users / manage_roles / manage_sso, short of super
admin) can't widen their own access: not by changing their own role, not by
editing the role they hold, and not by handing out a role or grant reaching
beyond what they hold themselves at that customer, folder or source."""

import uuid

import pytest
from app.api.auth import get_current_active_user
from app.api.roles import router as roles_router
from app.api.sso import router as sso_router
from app.api.users import router as users_router
from app.auth.models import (
    AuditLog,
    Capability,
    GlobalCapability,
    Role,
    RoleGrant,
    ScopeType,
    User,
)
from app.auth.rbac import (
    OWN_ROLE_REFUSED,
    ROLE_BEYOND_AUTHORITY,
    grant_within_authority,
    role_within_authority,
)
from app.db import get_session
from app.models import Customer, Folder, Protocol, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select
from structlog.testing import capture_logs

TEMP_PASSWORD = "Temporary-Pass-2026"

VIEW = [Capability.view]
VIEW_DOWNLOAD = [Capability.view, Capability.download]


def _role(session, *, global_capabilities=None, grants=(), is_super_admin=False) -> Role:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=is_super_admin,
        global_capabilities=global_capabilities or [],
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    for scope_type, scope_id, capabilities in grants:
        session.add(
            RoleGrant(
                role_id=role.id,
                scope_type=scope_type,
                scope_id=scope_id,
                capabilities=capabilities,
            )
        )
    session.commit()
    return role


def _user(session, role: Role) -> User:
    user = User(username=f"user-{uuid.uuid4().hex}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _client(session, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(users_router)
    app.include_router(roles_router)
    app.include_router(sso_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _customer(session, name) -> Customer:
    customer = Customer(name=name)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


def _assert_refused(response, detail=ROLE_BEYOND_AUTHORITY):
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == detail


def _assert_own_role_refused(response):
    _assert_refused(response, OWN_ROLE_REFUSED)


@pytest.fixture()
def customers(session):
    return _customer(session, "A"), _customer(session, "B")


def _admin(session, customer_a, *caps: GlobalCapability) -> User:
    """A delegated admin with view only in customer A."""
    role = _role(
        session,
        global_capabilities=list(caps),
        grants=[(ScopeType.customer, customer_a.id, VIEW)],
    )
    return _user(session, role)


# --- own role ----------------------------------------------------------------


def test_manage_users_cannot_change_their_own_role(session, customers):
    customer_a, _ = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_users)
    lesser = _role(session, global_capabilities=[GlobalCapability.manage_users])
    client = _client(session, admin)

    _assert_own_role_refused(client.patch(f"/users/{admin.id}", json={"role_id": lesser.id}))
    session.refresh(admin)
    assert admin.role_id != lesser.id
    # Their own record's other fields, and the same role, are still fine.
    assert client.patch(f"/users/{admin.id}", json={"role_id": admin.role_id}).status_code == 200


def test_super_admin_can_change_their_own_role(session, customers):
    super_role = _role(session, is_super_admin=True)
    admin = _user(session, super_role)
    _user(session, super_role)  # another super admin, so this isn't the last one
    other = _role(session)
    client = _client(session, admin)

    assert client.patch(f"/users/{admin.id}", json={"role_id": other.id}).status_code == 200


def test_manage_roles_cannot_edit_or_delete_the_role_they_hold(session, customers):
    customer_a, _ = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_roles)
    own = admin.role_id
    grant = session.exec(select(RoleGrant).where(RoleGrant.role_id == own)).one()
    client = _client(session, admin)

    _assert_own_role_refused(client.patch(f"/roles/{own}", json={"name": "mine"}))
    _assert_own_role_refused(client.delete(f"/roles/{own}"))
    _assert_own_role_refused(
        client.post(
            f"/roles/{own}/grants",
            json={"scope_type": "customer", "scope_id": customer_a.id, "capabilities": ["view"]},
        )
    )
    _assert_own_role_refused(
        client.patch(f"/roles/{own}/grants/{grant.id}", json={"capabilities": ["view"]})
    )
    _assert_own_role_refused(client.delete(f"/roles/{own}/grants/{grant.id}"))


def test_duplicating_the_role_they_hold_still_works(session, customers):
    customer_a, _ = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_roles)
    client = _client(session, admin)

    response = client.post(f"/roles/{admin.role_id}/duplicate", json={"name": "copy"})
    assert response.status_code == 201, response.text
    copy_id = response.json()["id"]
    # The copy isn't their own role, so they can edit it within their authority.
    assert client.patch(f"/roles/{copy_id}", json={"name": "copy 2"}).status_code == 200


# --- grants beyond the admin's own --------------------------------------------


def test_cannot_assign_a_role_with_grants_in_another_customer(session, customers):
    customer_a, customer_b = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_users)
    target = _user(session, _role(session))
    wider = _role(session, grants=[(ScopeType.customer, customer_b.id, VIEW)])
    client = _client(session, admin)

    _assert_refused(client.patch(f"/users/{target.id}", json={"role_id": wider.id}))
    _assert_refused(
        client.post(
            "/users",
            json={"username": "new@example.com", "password": TEMP_PASSWORD, "role_id": wider.id},
        )
    )


def test_cannot_assign_more_capabilities_than_held_in_the_same_customer(session, customers):
    customer_a, _ = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_users)
    target = _user(session, _role(session))
    more = _role(session, grants=[(ScopeType.customer, customer_a.id, VIEW_DOWNLOAD)])
    client = _client(session, admin)

    _assert_refused(client.patch(f"/users/{target.id}", json={"role_id": more.id}))


def test_assigning_within_their_own_access_still_works(session, customers):
    customer_a, _ = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_users)
    target = _user(session, _role(session))
    same = _role(session, grants=[(ScopeType.customer, customer_a.id, VIEW)])
    client = _client(session, admin)

    assert client.patch(f"/users/{target.id}", json={"role_id": same.id}).status_code == 200
    created = client.post(
        "/users",
        json={"username": "new@example.com", "password": TEMP_PASSWORD, "role_id": same.id},
    )
    assert created.status_code == 201, created.text


def test_cannot_manage_a_user_whose_role_reaches_beyond_theirs(session, customers):
    customer_a, customer_b = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_users)
    wider_user = _user(session, _role(session, grants=[(ScopeType.customer, customer_b.id, VIEW)]))
    client = _client(session, admin)

    _assert_refused(client.post(f"/users/{wider_user.id}/reset-password"))
    _assert_refused(client.delete(f"/users/{wider_user.id}"))


def test_cannot_create_a_grant_beyond_their_own(session, customers):
    customer_a, customer_b = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_roles)
    client = _client(session, admin)
    role_id = client.post("/roles", json={"name": "Tier 2"}).json()["id"]

    _assert_refused(
        client.post(
            f"/roles/{role_id}/grants",
            json={"scope_type": "customer", "scope_id": customer_b.id, "capabilities": ["view"]},
        )
    )
    _assert_refused(
        client.post(
            f"/roles/{role_id}/grants",
            json={
                "scope_type": "customer",
                "scope_id": customer_a.id,
                "capabilities": ["view", "download"],
            },
        )
    )
    ok = client.post(
        f"/roles/{role_id}/grants",
        json={"scope_type": "customer", "scope_id": customer_a.id, "capabilities": ["view"]},
    )
    assert ok.status_code == 201, ok.text
    _assert_refused(
        client.patch(
            f"/roles/{role_id}/grants/{ok.json()['id']}",
            json={"capabilities": ["view", "download"]},
        )
    )


def test_cannot_duplicate_or_edit_a_role_reaching_beyond_theirs(session, customers):
    customer_a, customer_b = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_roles)
    wider = _role(session, grants=[(ScopeType.customer, customer_b.id, VIEW)])
    more = _role(session, grants=[(ScopeType.customer, customer_a.id, VIEW_DOWNLOAD)])
    client = _client(session, admin)

    for role in (wider, more):
        _assert_refused(client.post(f"/roles/{role.id}/duplicate", json={"name": f"c{role.id}"}))
        _assert_refused(client.patch(f"/roles/{role.id}", json={"name": "x"}))
        _assert_refused(client.delete(f"/roles/{role.id}"))


def test_cannot_map_an_sso_group_to_a_role_reaching_beyond_theirs(session, customers):
    customer_a, customer_b = customers
    admin = _admin(session, customer_a, GlobalCapability.manage_sso)
    wider = _role(session, grants=[(ScopeType.customer, customer_b.id, VIEW)])
    same = _role(session, grants=[(ScopeType.customer, customer_a.id, VIEW)])
    client = _client(session, admin)

    _assert_refused(
        client.post(
            "/sso/group-mappings", json={"order": 0, "group_name": "ops", "role_id": wider.id}
        )
    )
    ok = client.post(
        "/sso/group-mappings", json={"order": 0, "group_name": "ops", "role_id": same.id}
    )
    assert ok.status_code == 201, ok.text


# --- grant resolution at each scope -------------------------------------------


def _source(session, customer, folder=None) -> Source:
    source = Source(
        name=f"s-{uuid.uuid4().hex[:6]}",
        customer_id=customer.id,
        folder_id=folder.id if folder else None,
        protocol=Protocol.ssh,
        host="h",
        base_path="/var/log",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def test_a_grant_above_covers_narrower_scopes_beneath_it(session, customers):
    customer_a, customer_b = customers
    folder = Folder(name="prod", customer_id=customer_a.id)
    session.add(folder)
    session.commit()
    session.refresh(folder)
    in_folder = _source(session, customer_a, folder)
    elsewhere = _source(session, customer_b)
    admin = _admin(session, customer_a, GlobalCapability.manage_roles)

    def within(scope_type, scope_id, caps):
        return grant_within_authority(
            session, admin, scope_type=scope_type, scope_id=scope_id, capabilities=caps
        )

    assert within(ScopeType.folder, folder.id, VIEW)
    assert within(ScopeType.source, in_folder.id, VIEW)
    assert not within(ScopeType.source, in_folder.id, VIEW_DOWNLOAD)
    assert not within(ScopeType.source, elsewhere.id, VIEW)
    assert not within(ScopeType.customer, customer_b.id, VIEW)
    assert within(ScopeType.customer, customer_b.id, [])


def test_a_narrower_grant_of_the_admin_bounds_what_they_can_hand_out_there(session, customers):
    customer_a, _ = customers
    folder = Folder(name="prod", customer_id=customer_a.id)
    session.add(folder)
    session.commit()
    session.refresh(folder)
    role = _role(
        session,
        global_capabilities=[GlobalCapability.manage_roles],
        grants=[
            (ScopeType.customer, customer_a.id, VIEW_DOWNLOAD),
            (ScopeType.folder, folder.id, VIEW),
        ],
    )
    admin = _user(session, role)

    assert grant_within_authority(
        session, admin, scope_type=ScopeType.customer, scope_id=customer_a.id, capabilities=VIEW
    )
    assert not grant_within_authority(
        session,
        admin,
        scope_type=ScopeType.folder,
        scope_id=folder.id,
        capabilities=VIEW_DOWNLOAD,
    )


def _folder(session, customer, parent=None) -> Folder:
    folder = Folder(
        name=f"f-{uuid.uuid4().hex[:6]}",
        customer_id=customer.id,
        parent_folder_id=parent.id if parent else None,
    )
    session.add(folder)
    session.commit()
    session.refresh(folder)
    return folder


def test_a_narrower_grant_beneath_bounds_a_wider_grant(session, customers):
    """View in customer A, and a grant on folder A/x without view: handing out
    view on the whole of A would reach into A/x, which the admin can't see."""
    customer_a, _ = customers
    folder_x = _folder(session, customer_a)
    admin = _user(
        session,
        _role(
            session,
            global_capabilities=[GlobalCapability.manage_roles],
            grants=[
                (ScopeType.customer, customer_a.id, VIEW),
                (ScopeType.folder, folder_x.id, [Capability.download]),
            ],
        ),
    )
    client = _client(session, admin)
    role_id = client.post("/roles", json={"name": "Tier 2"}).json()["id"]

    _assert_refused(
        client.post(
            f"/roles/{role_id}/grants",
            json={"scope_type": "customer", "scope_id": customer_a.id, "capabilities": ["view"]},
        )
    )
    other = _role(session, grants=[(ScopeType.customer, customer_a.id, VIEW)])
    assert not role_within_authority(session, admin, other)


def test_without_the_narrower_grant_the_wider_grant_is_within(session, customers):
    customer_a, _ = customers
    _folder(session, customer_a)
    admin = _admin(session, customer_a, GlobalCapability.manage_roles)
    client = _client(session, admin)
    role_id = client.post("/roles", json={"name": "Tier 2"}).json()["id"]

    ok = client.post(
        f"/roles/{role_id}/grants",
        json={"scope_type": "customer", "scope_id": customer_a.id, "capabilities": ["view"]},
    )
    assert ok.status_code == 201, ok.text


def test_a_narrower_grant_deeper_down_also_bounds_a_folder_grant(session, customers):
    customer_a, _ = customers
    parent = _folder(session, customer_a)
    child = _folder(session, customer_a, parent)
    source = _source(session, customer_a, child)
    admin = _user(
        session,
        _role(
            session,
            grants=[
                (ScopeType.customer, customer_a.id, VIEW_DOWNLOAD),
                (ScopeType.source, source.id, VIEW),
            ],
        ),
    )

    def within(scope_type, scope_id, caps, role_id=None):
        return grant_within_authority(
            session,
            admin,
            scope_type=scope_type,
            scope_id=scope_id,
            capabilities=caps,
            role_id=role_id,
        )

    assert within(ScopeType.folder, parent.id, VIEW)
    assert not within(ScopeType.folder, parent.id, VIEW_DOWNLOAD)
    assert not within(ScopeType.customer, customer_a.id, VIEW_DOWNLOAD)
    # A role whose own grant already governs that source (or a folder above
    # it, beneath the grant being checked) isn't widened there by it.
    governed = _role(session, grants=[(ScopeType.folder, child.id, VIEW)])
    assert within(ScopeType.customer, customer_a.id, VIEW_DOWNLOAD, governed.id)
    assert not within(ScopeType.folder, child.id, VIEW_DOWNLOAD, governed.id)


def test_duplicating_their_own_role_with_narrower_grants_still_works(session, customers):
    customer_a, _ = customers
    folder_x = _folder(session, customer_a)
    admin = _user(
        session,
        _role(
            session,
            global_capabilities=[GlobalCapability.manage_roles],
            grants=[
                (ScopeType.customer, customer_a.id, VIEW),
                (ScopeType.folder, folder_x.id, [Capability.download]),
            ],
        ),
    )
    client = _client(session, admin)

    response = client.post(f"/roles/{admin.role_id}/duplicate", json={"name": "copy"})
    assert response.status_code == 201, response.text


def test_removing_a_narrower_grant_cannot_widen_the_role_beyond_theirs(session, customers):
    """The role's own empty grant on A/x is what lets view on the whole of A
    through; deleting it would hand A/x, which the admin can't see, to
    everyone holding the role. The role as it would stand is checked, and
    stays as it was."""
    customer_a, _ = customers
    folder_x = _folder(session, customer_a)
    admin = _user(
        session,
        _role(
            session,
            global_capabilities=[GlobalCapability.manage_roles, GlobalCapability.manage_users],
            grants=[
                (ScopeType.customer, customer_a.id, VIEW),
                (ScopeType.folder, folder_x.id, []),
            ],
        ),
    )
    client = _client(session, admin)
    role_id = client.post("/roles", json={"name": "Tier 2"}).json()["id"]
    narrower = client.post(
        f"/roles/{role_id}/grants",
        json={"scope_type": "folder", "scope_id": folder_x.id, "capabilities": []},
    )
    assert narrower.status_code == 201, narrower.text
    wider = client.post(
        f"/roles/{role_id}/grants",
        json={"scope_type": "customer", "scope_id": customer_a.id, "capabilities": ["view"]},
    )
    assert wider.status_code == 201, wider.text

    with capture_logs() as logs:
        _assert_refused(client.delete(f"/roles/{role_id}/grants/{narrower.json()['id']}"))
    # Refused before anything is recorded: no log line and no audit row for
    # a change that never happened.
    assert not [entry for entry in logs if entry["event"].startswith("role_grant.")]
    assert (
        session.exec(select(AuditLog).where(AuditLog.action == "role_grant.delete")).first() is None
    )
    grants = {
        (g.scope_type, g.scope_id): list(g.capabilities)
        for g in session.exec(select(RoleGrant).where(RoleGrant.role_id == role_id)).all()
    }
    assert grants == {
        (ScopeType.folder, folder_x.id): [],
        (ScopeType.customer, customer_a.id): VIEW,
    }
    # Once the wider grant is gone, the narrower one can go too.
    assert client.delete(f"/roles/{role_id}/grants/{wider.json()['id']}").status_code == 204
    assert client.delete(f"/roles/{role_id}/grants/{narrower.json()['id']}").status_code == 204
    # The deleted grant is still named in its audit entry.
    deleted = session.exec(
        select(AuditLog)
        .where(AuditLog.action == "role_grant.delete")
        .where(AuditLog.target_id == narrower.json()["id"])
    ).one()
    assert deleted.event_metadata["target_name"] == f"folder {folder_x.name}"
    assert deleted.event_metadata["role_name"] == "Tier 2"


def test_super_admin_is_never_limited(session, customers):
    _, customer_b = customers
    admin = _user(session, _role(session, is_super_admin=True))
    wider = _role(session, grants=[(ScopeType.customer, customer_b.id, VIEW_DOWNLOAD)])

    assert role_within_authority(session, admin, wider)
