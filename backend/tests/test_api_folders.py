import pytest
from app.api.auth import get_current_active_user
from app.api.folders import router as folders_router
from app.auth.models import Role, User
from app.db import get_session
from app.models import Customer
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_user(session, *, is_super_admin=False, global_capabilities=None) -> User:
    role = Role(
        name=f"role-{is_super_admin}-{global_capabilities}",
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
    return user


def _make_customer(session, name="Vodacom Tanzania") -> Customer:
    customer = Customer(name=name)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(folders_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


@pytest.fixture()
def admin_client(session, client_for):
    return client_for(_make_user(session, is_super_admin=True))


def test_create_and_list_folders(session, admin_client):
    customer = _make_customer(session)

    created = admin_client.post("/folders", json={"name": "Production", "customer_id": customer.id})
    assert created.status_code == 201
    assert created.json()["parent_folder_id"] is None

    listed = admin_client.get(f"/folders?customer_id={customer.id}")
    assert len(listed.json()) == 1


def test_nested_subfolder(session, admin_client):
    customer = _make_customer(session)
    root = admin_client.post(
        "/folders", json={"name": "Production", "customer_id": customer.id}
    ).json()

    child = admin_client.post(
        "/folders",
        json={"name": "app-servers", "customer_id": customer.id, "parent_folder_id": root["id"]},
    )
    assert child.status_code == 201
    assert child.json()["parent_folder_id"] == root["id"]


def test_parent_must_be_same_customer(session, admin_client):
    customer_a = _make_customer(session, "Customer A")
    customer_b = _make_customer(session, "Customer B")
    root = admin_client.post("/folders", json={"name": "root", "customer_id": customer_a.id}).json()

    response = admin_client.post(
        "/folders",
        json={"name": "child", "customer_id": customer_b.id, "parent_folder_id": root["id"]},
    )
    assert response.status_code == 400


def test_cannot_move_folder_under_its_own_descendant(session, admin_client):
    customer = _make_customer(session)
    root = admin_client.post("/folders", json={"name": "root", "customer_id": customer.id}).json()
    child = admin_client.post(
        "/folders",
        json={"name": "child", "customer_id": customer.id, "parent_folder_id": root["id"]},
    ).json()

    response = admin_client.patch(f"/folders/{root['id']}", json={"parent_folder_id": child["id"]})
    assert response.status_code == 400


def test_rename_folder(session, admin_client):
    customer = _make_customer(session)
    folder = admin_client.post("/folders", json={"name": "old", "customer_id": customer.id}).json()

    response = admin_client.patch(f"/folders/{folder['id']}", json={"name": "new"})
    assert response.status_code == 200
    assert response.json()["name"] == "new"


def test_cannot_delete_folder_with_children_or_sources(session, admin_client):
    customer = _make_customer(session)
    root = admin_client.post("/folders", json={"name": "root", "customer_id": customer.id}).json()
    admin_client.post(
        "/folders",
        json={"name": "child", "customer_id": customer.id, "parent_folder_id": root["id"]},
    )

    response = admin_client.delete(f"/folders/{root['id']}")
    assert response.status_code == 409


def test_delete_leaf_folder(session, admin_client):
    customer = _make_customer(session)
    folder = admin_client.post("/folders", json={"name": "leaf", "customer_id": customer.id}).json()

    response = admin_client.delete(f"/folders/{folder['id']}")
    assert response.status_code == 204


def test_plain_user_cannot_manage_folders(session, client_for):
    user = _make_user(session)
    client = client_for(user)
    assert client.get("/folders").status_code == 403
