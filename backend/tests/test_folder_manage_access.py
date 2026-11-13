"""create_source lets a role add and rename folders; moving or deleting one
also needs manage_rules on it (and, to move it, where it goes)."""

import uuid

import pytest
from app.api.auth import get_current_active_user
from app.api.folders import MANAGE_FOLDER_REFUSED
from app.api.folders import router as folders_router
from app.auth.models import Capability, GlobalCapability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Customer, Folder
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _role(session, *, is_super_admin=False, grants=()) -> Role:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=is_super_admin,
        global_capabilities=[] if is_super_admin else [GlobalCapability.create_source],
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    for scope_type, scope_id, capabilities in grants:
        session.add(
            RoleGrant(
                role_id=role.id, scope_type=scope_type, scope_id=scope_id, capabilities=capabilities
            )
        )
    session.commit()
    return role


def _client(session, role: Role) -> TestClient:
    user = User(username=f"user-{uuid.uuid4().hex}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    app = FastAPI()
    app.include_router(folders_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _folder(session, customer, name, parent=None) -> Folder:
    folder = Folder(
        name=name, customer_id=customer.id, parent_folder_id=parent.id if parent else None
    )
    session.add(folder)
    session.commit()
    session.refresh(folder)
    return folder


@pytest.fixture()
def tree(session):
    """A customer with two top-level folders: X (to move) and Y (a target)."""
    customer = Customer(name="A")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer, _folder(session, customer, "X"), _folder(session, customer, "Y")


VIEW = [Capability.view]
MANAGE = [Capability.view, Capability.manage_rules]


def _assert_refused(response):
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == MANAGE_FOLDER_REFUSED


def test_cannot_pull_a_folder_it_cannot_reach_under_one_it_can(session, tree):
    _, x, y = tree
    client = _client(session, _role(session, grants=[(ScopeType.folder, y.id, VIEW)]))

    _assert_refused(client.patch(f"/folders/{x.id}", json={"parent_folder_id": y.id}))
    session.refresh(x)
    assert x.parent_folder_id is None


def test_moving_needs_manage_rules_at_the_destination(session, tree):
    _, x, y = tree
    role = _role(session, grants=[(ScopeType.folder, x.id, MANAGE), (ScopeType.folder, y.id, VIEW)])
    client = _client(session, role)

    _assert_refused(
        client.patch(f"/folders/{x.id}", json={"name": "renamed", "parent_folder_id": y.id})
    )
    session.refresh(x)
    assert (x.parent_folder_id, x.name) == (None, "X")


def test_manage_rules_on_folder_and_destination_allows_moving(session, tree):
    _, x, y = tree
    role = _role(
        session, grants=[(ScopeType.folder, x.id, MANAGE), (ScopeType.folder, y.id, MANAGE)]
    )
    client = _client(session, role)

    response = client.patch(f"/folders/{x.id}", json={"parent_folder_id": y.id})
    assert response.status_code == 200, response.text
    session.refresh(x)
    assert x.parent_folder_id == y.id


def test_moving_to_the_root_needs_manage_rules_on_the_customer(session, tree):
    customer, x, y = tree
    child = _folder(session, customer, "child", parent=y)

    refused = _client(
        session,
        _role(
            session,
            grants=[(ScopeType.customer, customer.id, VIEW), (ScopeType.folder, y.id, MANAGE)],
        ),
    )
    _assert_refused(refused.patch(f"/folders/{child.id}", json={"parent_folder_id": None}))
    session.refresh(child)
    assert child.parent_folder_id == y.id

    allowed = _client(session, _role(session, grants=[(ScopeType.customer, customer.id, MANAGE)]))
    response = allowed.patch(f"/folders/{child.id}", json={"parent_folder_id": None})
    assert response.status_code == 200, response.text
    session.refresh(child)
    assert child.parent_folder_id is None


def test_destination_resolution_walks_the_target_folder_chain(session, tree):
    """The most specific grant wins: a view-only grant on a sub-folder refuses
    a move into it even under a customer-wide manage_rules grant."""
    customer, x, y = tree
    locked = _folder(session, customer, "locked", parent=y)
    open_child = _folder(session, customer, "open", parent=y)
    role = _role(
        session,
        grants=[(ScopeType.customer, customer.id, MANAGE), (ScopeType.folder, locked.id, VIEW)],
    )
    client = _client(session, role)

    _assert_refused(client.patch(f"/folders/{x.id}", json={"parent_folder_id": locked.id}))
    moved = client.patch(f"/folders/{x.id}", json={"parent_folder_id": open_child.id})
    assert moved.status_code == 200, moved.text


FULL = [Capability.view, Capability.download, Capability.manage_rules]


def test_a_move_cannot_widen_the_movers_access(session, tree):
    """With no grant of its own, a folder inherits from where it lands: a
    destination granting more than where it is now is refused, even with
    manage_rules on both."""
    customer, x, y = tree
    role = _role(
        session,
        grants=[
            (ScopeType.customer, customer.id, [Capability.manage_rules]),
            (ScopeType.folder, y.id, FULL),
        ],
    )
    client = _client(session, role)

    _assert_refused(client.patch(f"/folders/{x.id}", json={"parent_folder_id": y.id}))
    session.refresh(x)
    assert x.parent_folder_id is None


def test_a_move_to_the_same_or_less_access_is_allowed(session, tree):
    customer, x, y = tree
    same = _folder(session, customer, "same")
    role = _role(
        session,
        grants=[
            (ScopeType.customer, customer.id, FULL),
            (ScopeType.folder, y.id, [Capability.manage_rules]),
            (ScopeType.folder, same.id, FULL),
        ],
    )
    client = _client(session, role)

    to_less = client.patch(f"/folders/{x.id}", json={"parent_folder_id": y.id})
    assert to_less.status_code == 200, to_less.text
    other = _folder(session, customer, "other")
    to_same = client.patch(f"/folders/{other.id}", json={"parent_folder_id": same.id})
    assert to_same.status_code == 200, to_same.text


def test_a_folder_with_its_own_grant_can_move_to_more_access(session, tree):
    """Its own grant keeps governing it, so the move changes nothing for the mover."""
    _, x, y = tree
    role = _role(
        session,
        grants=[
            (ScopeType.folder, x.id, [Capability.manage_rules]),
            (ScopeType.folder, y.id, FULL),
        ],
    )
    client = _client(session, role)

    response = client.patch(f"/folders/{x.id}", json={"parent_folder_id": y.id})
    assert response.status_code == 200, response.text


def test_deleting_needs_manage_rules_on_the_folder(session, tree):
    _, x, y = tree
    role = _role(session, grants=[(ScopeType.folder, x.id, VIEW), (ScopeType.folder, y.id, MANAGE)])
    client = _client(session, role)

    _assert_refused(client.delete(f"/folders/{x.id}"))
    assert session.get(Folder, x.id) is not None
    assert client.delete(f"/folders/{y.id}").status_code == 204


def test_renaming_needs_only_create_source(session, tree):
    _, x, _ = tree
    client = _client(session, _role(session))

    response = client.patch(f"/folders/{x.id}", json={"name": "renamed"})
    assert response.status_code == 200, response.text
    # Naming the parent it already has is not a move.
    same = client.patch(f"/folders/{x.id}", json={"name": "again", "parent_folder_id": None})
    assert same.status_code == 200, same.text


def test_super_admin_can_still_do_everything(session, tree):
    _, x, y = tree
    client = _client(session, _role(session, is_super_admin=True))

    assert client.patch(f"/folders/{x.id}", json={"parent_folder_id": y.id}).status_code == 200
    assert client.patch(f"/folders/{x.id}", json={"parent_folder_id": None}).status_code == 200
    assert client.delete(f"/folders/{x.id}").status_code == 204
