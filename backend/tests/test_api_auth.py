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
    role = Role(name=f"Support-{id(object())}")
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


def test_me_includes_role_capabilities_for_frontend_nav_gating(client, session):
    from app.auth.models import GlobalCapability

    role = Role(name="Manager", global_capabilities=[GlobalCapability.manage_users])
    session.add(role)
    session.commit()
    session.refresh(role)
    create_local_user(
        session, actor_user_id=None, username="mgr@example.com", password=PASSWORD, role_id=role.id
    )
    _login(client, username="mgr@example.com")

    response = client.get("/auth/me")
    assert response.json()["is_super_admin"] is False
    assert response.json()["global_capabilities"] == ["manage_users"]


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


def test_login_locks_out_after_repeated_failures(client, session, monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    _make_user(session)

    for _ in range(3):
        response = _login(client, password="wrong")
        assert response.status_code == 401

    locked_response = _login(client, password=PASSWORD)  # even the right password now
    assert locked_response.status_code == 429
    assert "Retry-After" in locked_response.headers

    get_settings.cache_clear()


def test_login_lockout_is_scoped_to_the_attempted_username(client, session, monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "1")
    get_settings.cache_clear()
    _make_user(session)
    _make_user(session, username="other@example.com")

    locked_out = _login(client, password="wrong")
    assert locked_out.status_code == 401

    still_open = _login(client, username="other@example.com", password="wrong")
    assert still_open.status_code == 401  # not 429 -- different username

    get_settings.cache_clear()


def test_successful_login_clears_prior_failures(client, session, monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    _make_user(session)

    _login(client, password="wrong")
    _login(client, password="wrong")
    ok_response = _login(client, password=PASSWORD)
    assert ok_response.status_code == 200

    # Two more wrong attempts shouldn't lock out -- the successful login
    # above should have reset the failure count back to zero.
    _login(client, password="wrong")
    second_wrong = _login(client, password="wrong")
    assert second_wrong.status_code == 401

    get_settings.cache_clear()


def test_list_sessions_requires_authentication(client):
    response = client.get("/auth/sessions")
    assert response.status_code == 401


def test_list_sessions_returns_the_current_session_marked_as_current(client, session):
    _make_user(session)
    _login(client)

    response = client.get("/auth/sessions")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["is_current"] is True
    # TestClient sends its own default User-Agent when none is set
    # explicitly -- confirms the header is captured at all, not that it's
    # ever actually absent.
    assert body[0]["user_agent"]


def test_list_sessions_captures_the_user_agent(client, session):
    _make_user(session)
    client.post(
        "/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"User-Agent": "TestBrowser/1.0"},
    )

    response = client.get("/auth/sessions")
    assert response.json()[0]["user_agent"] == "TestBrowser/1.0"


def test_list_sessions_only_shows_the_current_users_own_sessions(client, session):
    _make_user(session)
    _make_user(session, username="other@example.com")
    _login(client, username="other@example.com")

    response = client.get("/auth/sessions")
    assert len(response.json()) == 1


def test_revoke_session_requires_authentication(client):
    response = client.delete("/auth/sessions/1")
    assert response.status_code == 401


def test_revoke_session_removes_it_from_the_list(client, session):
    _make_user(session)
    _login(client)
    session_id = client.get("/auth/sessions").json()[0]["id"]

    response = client.delete(f"/auth/sessions/{session_id}")
    assert response.status_code == 204

    # The session used to make this very request was just revoked, so the
    # client is effectively logged out now -- the next call is unauthenticated.
    assert client.get("/auth/sessions").status_code == 401


def test_revoke_session_returns_404_for_a_session_owned_by_someone_else(client, session):
    _make_user(session)
    _make_user(session, username="other@example.com")

    _login(client, username="other@example.com")
    other_session_id = client.get("/auth/sessions").json()[0]["id"]
    # Logging in again (as this test's real subject) overwrites this
    # client's cookie without touching the "other" session row above, same
    # as a second real browser holding its own independent cookie would.
    _login(client)

    response = client.delete(f"/auth/sessions/{other_session_id}")
    assert response.status_code == 404


def test_revoke_session_returns_404_for_an_unknown_id(client, session):
    _make_user(session)
    _login(client)

    response = client.delete("/auth/sessions/999999")
    assert response.status_code == 404


def test_logging_in_twice_shows_both_sessions_with_only_the_newest_current(client, session):
    _make_user(session)
    _login(client)  # first "device"
    _login(client)  # second "device" -- overwrites this client's cookie, same as a real
    # second browser would independently hold its own

    response = client.get("/auth/sessions")
    body = response.json()
    assert len(body) == 2
    assert sum(1 for s in body if s["is_current"]) == 1
