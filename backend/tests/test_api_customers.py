import pytest
from app.api.auth import get_current_active_user
from app.api.customers import router as customers_router
from app.auth.models import GlobalCapability, Role, User
from app.db import get_session
from app.models import Customer, Folder, Protocol, Source
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


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(customers_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


def test_create_source_capability_can_create_and_list_customers(session, client_for):
    user = _make_user(session, global_capabilities=[GlobalCapability.create_source])
    client = client_for(user)

    create = client.post("/customers", json={"name": "Vodacom Tanzania"})
    assert create.status_code == 201
    assert create.json()["name"] == "Vodacom Tanzania"

    listed = client.get("/customers")
    assert listed.status_code == 200
    assert [c["name"] for c in listed.json()] == ["Vodacom Tanzania"]


def test_plain_user_cannot_manage_customers(session, client_for):
    user = _make_user(session)
    client = client_for(user)

    assert client.get("/customers").status_code == 403
    assert client.post("/customers", json={"name": "X"}).status_code == 403


def test_super_admin_can_manage_customers(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    create = client.post("/customers", json={"name": "Fidelidade"})
    assert create.status_code == 201


def test_duplicate_customer_name_rejected(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    client.post("/customers", json={"name": "GermanCloud"})
    dupe = client.post("/customers", json={"name": "GermanCloud"})
    assert dupe.status_code == 409


def test_update_and_delete_customer(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    created = client.post("/customers", json={"name": "Old Name"}).json()

    updated = client.patch(f"/customers/{created['id']}", json={"name": "New Name"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "New Name"

    deleted = client.delete(f"/customers/{created['id']}")
    assert deleted.status_code == 204
    assert client.get("/customers").json() == []


def test_cannot_delete_customer_with_sources(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    customer = Customer(name="Has Source")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    session.add(
        Source(
            name="app01",
            customer_id=customer.id,
            protocol=Protocol.ssh,
            host="app01",
            base_path="/var/log",
        )
    )
    session.commit()

    response = client.delete(f"/customers/{customer.id}")
    assert response.status_code == 409


def test_cannot_delete_customer_with_folders(session, client_for):
    user = _make_user(session, is_super_admin=True)
    client = client_for(user)

    customer = Customer(name="Has Folder")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    session.add(Folder(name="env", customer_id=customer.id))
    session.commit()

    response = client.delete(f"/customers/{customer.id}")
    assert response.status_code == 409
