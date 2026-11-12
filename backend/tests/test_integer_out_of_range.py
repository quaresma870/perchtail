"""#376: an integer above SQLite's signed 64-bit range in the path or body is
refused with 422 instead of crashing with a 500; any other OverflowError is
still a server error."""

import uuid

import pytest
from app.api.auth import get_current_active_user
from app.api.errors import install as install_error_handlers
from app.api.folders import router as folders_router
from app.api.sources import router as sources_router
from app.auth.models import Role, User
from app.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOO_LARGE = 9223372036854775808  # 2**63
MESSAGE = {"detail": "A number in the request is out of range"}


@pytest.fixture()
def app_and_client(session):
    role = Role(name=f"super-{uuid.uuid4().hex}", is_super_admin=True)
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"admin-{role.id}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)

    app = FastAPI()
    app.include_router(sources_router)
    app.include_router(folders_router)
    install_error_handlers(app)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return app, TestClient(app, raise_server_exceptions=False)


def test_path_id_out_of_range_is_422(app_and_client):
    _, client = app_and_client
    response = client.get(f"/sources/{TOO_LARGE}")
    assert response.status_code == 422
    assert response.json() == MESSAGE


def test_body_id_out_of_range_is_422(app_and_client):
    _, client = app_and_client
    response = client.post("/folders", json={"name": "logs", "customer_id": TOO_LARGE})
    assert response.status_code == 422
    assert response.json() == MESSAGE


def test_source_port_out_of_range_is_422(app_and_client):
    _, client = app_and_client
    response = client.post(
        "/sources",
        json={
            "name": "app01",
            "protocol": "ssh",
            "host": "app01.example.com",
            "port": TOO_LARGE,
            "base_path": "/var/log/appname",
        },
    )
    assert response.status_code == 422
    assert response.json() == MESSAGE


def test_other_overflow_errors_stay_server_errors(app_and_client):
    app, client = app_and_client

    @app.get("/overflow")
    def _overflow():
        raise OverflowError("math range error")

    response = client.get("/overflow")
    assert response.status_code == 500
