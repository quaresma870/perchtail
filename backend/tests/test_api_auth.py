import pytest
from app.api.auth import get_current_active_user
from app.api.auth import router as auth_router
from app.auth.models import Role, User
from app.auth.providers.local import create_local_user
from app.config import get_settings
from app.db import get_session
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

USERNAME = "jdoe@example.com"
PASSWORD = "s3cret!"


@pytest.fixture()
def client(session, monkeypatch):
    # TestClient talks plain HTTP; a Secure cookie (the production default)
    # would be set but never sent back by the client's cookie jar, so
    # disable it here rather than for real deployments.
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(auth_router)

    @app.get("/protected")
    def protected(user=Depends(get_current_active_user)):
        return {"username": user.username}

    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)

    get_settings.cache_clear()


def _make_user(session, *, username: str = USERNAME, password: str = PASSWORD) -> User:
    role = Role(name="Support")
    session.add(role)
    session.commit()
    session.refresh(role)
    return create_local_user(
        session, actor_user_id=None, username=username, password=password, role_id=role.id
    )


def _login(client, *, username: str = USERNAME, password: str = PASSWORD):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_login_sets_cookie_and_returns_user(client, session):
    _make_user(session)

    response = _login(client)

    assert response.status_code == 200
    assert response.json()["username"] == USERNAME
    assert response.json()["must_change_password"] is True
    assert "perchtail_session" in response.cookies


def test_login_rejects_wrong_password(client, session):
    _make_user(session)

    response = _login(client, password="wrong")
    assert response.status_code == 401
    assert "perchtail_session" not in response.cookies


def test_me_requires_authentication(client):
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_me_returns_current_user_after_login(client, session):
    _make_user(session)
    _login(client)

    response = client.get("/auth/me")
    assert response.status_code == 200
    assert response.json()["username"] == USERNAME


def test_logout_revokes_the_session(client, session):
    _make_user(session)
    _login(client)
    assert client.get("/auth/me").status_code == 200

    logout_response = client.post("/auth/logout")
    assert logout_response.status_code == 204

    assert client.get("/auth/me").status_code == 401


def test_change_password_clears_flag_and_allows_relogin(client, session):
    _make_user(session)
    _login(client)

    response = client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "new-password!"},
    )
    assert response.status_code == 200
    assert response.json()["must_change_password"] is False

    client.post("/auth/logout")
    relogin = _login(client, password="new-password!")
    assert relogin.status_code == 200


def test_active_user_dependency_blocks_until_password_changed(client, session):
    _make_user(session)
    _login(client)

    blocked = client.get("/protected")
    assert blocked.status_code == 403

    client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "new-password!"},
    )

    allowed = client.get("/protected")
    assert allowed.status_code == 200


def test_change_password_rejects_wrong_current_password(client, session):
    _make_user(session)
    _login(client)

    response = client.post(
        "/auth/change-password",
        json={"current_password": "wrong", "new_password": "new-password!"},
    )
    assert response.status_code == 401
