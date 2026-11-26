"""HTTP and configuration: __Host- cookie names with Secure cookies,
no-store on every API answer, the key from a file, rejected requests in the
log, Clear-Site-Data on sign-out, and downloads as octet-stream."""

import pytest
from app.api import auth as auth_api
from app.config import Settings, get_settings
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

# -- cookie names -------------------------------------------------------------------


def test_secure_cookies_carry_the_host_prefix(monkeypatch):
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    get_settings.cache_clear()
    try:
        assert auth_api.session_cookie_name() == "__Host-perchtail_session"
        assert auth_api.sso_state_cookie_name() == "__Host-perchtail_sso_state"
    finally:
        get_settings.cache_clear()


def test_without_secure_the_names_stay_plain(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    try:
        assert auth_api.session_cookie_name() == "perchtail_session"
        assert auth_api.sso_state_cookie_name() == "perchtail_sso_state"
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def app_client(session, monkeypatch, tmp_path):
    """The whole app, as deployed: Secure cookies, over https."""
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    get_settings.cache_clear()
    from app.db import get_session
    from app.main import app

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app, base_url="https://testserver")
    yield client
    # Only what this fixture set: other modules override the same app.
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    get_settings.cache_clear()


def _admin(session, password="Unrelated-Horse-9-Battery"):
    from app.auth.models import Role
    from app.auth.providers.local import create_local_user

    role = Role(name="viewer-role")
    session.add(role)
    session.commit()
    session.refresh(role)
    return create_local_user(
        session,
        actor_user_id=None,
        username="viewer@example.com",
        password=password,
        role_id=role.id,
    )


def test_sign_in_sets_a_host_prefixed_session_cookie(session, app_client):
    _admin(session)
    answer = app_client.post(
        "/auth/login",
        json={"username": "viewer@example.com", "password": "Unrelated-Horse-9-Battery"},
    )
    assert answer.status_code == 200, answer.text
    set_cookie = answer.headers["set-cookie"]
    assert set_cookie.startswith("__Host-perchtail_session=")
    for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=strict"):
        assert attribute.lower() in set_cookie.lower()
    assert "domain=" not in set_cookie.lower()
    # The new name is the one read back; the old name is ignored.
    assert app_client.get("/auth/me").status_code == 200
    token = app_client.cookies.get("__Host-perchtail_session")
    app_client.cookies.clear()
    app_client.cookies.set("perchtail_session", token)
    assert app_client.get("/auth/me").status_code == 401


def test_sign_out_clears_the_cookie_and_the_cache(session, app_client):
    _admin(session)
    app_client.post(
        "/auth/login",
        json={"username": "viewer@example.com", "password": "Unrelated-Horse-9-Battery"},
    )
    answer = app_client.post("/auth/logout")
    assert answer.status_code == 204
    assert answer.headers["clear-site-data"] == '"cache"'
    set_cookie = answer.headers["set-cookie"].lower()
    assert set_cookie.startswith("__host-perchtail_session=")
    assert "secure" in set_cookie and "path=/" in set_cookie and "max-age=0" in set_cookie


# -- no-store -------------------------------------------------------------------------


def test_every_api_answer_is_no_store(session, app_client):
    for path in ("/auth/me", "/auth/login-options", "/sources", "/does-not-exist-api"):
        answer = app_client.get(path)
        assert answer.headers.get("cache-control") == "no-store", (path, answer.status_code)


def test_an_answer_with_its_own_cache_control_keeps_it():
    from app.main import SecurityHeadersMiddleware  # noqa: F401 - the rule is in it
    from fastapi import FastAPI, Response
    from starlette.middleware.base import BaseHTTPMiddleware

    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=SecurityHeadersMiddleware(app).dispatch)

    @app.get("/cached")
    def cached():
        return Response("x", headers={"Cache-Control": "max-age=60"})

    assert TestClient(app).get("/cached").headers["cache-control"] == "max-age=60"


def test_the_frontends_static_files_keep_their_caching(tmp_path):
    from app.main import SecurityHeadersMiddleware
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.base import BaseHTTPMiddleware

    (tmp_path / "index.html").write_text("<title>PerchTail</title>")
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=SecurityHeadersMiddleware(app).dispatch)
    app.mount("/", StaticFiles(directory=tmp_path, html=True), name="frontend")
    answer = TestClient(app).get("/")
    assert answer.status_code == 200
    assert answer.headers.get("cache-control") != "no-store"
    # An unknown path falls through to the mount; its 404 is no-store.
    missing = TestClient(app).get("/does-not-exist-api")
    assert missing.status_code == 404
    assert missing.headers.get("cache-control") == "no-store"


# -- CREDENTIAL_ENCRYPTION_KEY_FILE ---------------------------------------------------


KEY = "k" * 43


def test_the_key_comes_from_a_file_without_its_final_line_break(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    for content in (KEY + "\n", KEY + "\r\n", KEY):
        key_file = tmp_path / "key"
        key_file.write_bytes(content.encode())
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_FILE", str(key_file))
        assert Settings(_env_file=None).credential_encryption_key == KEY


def test_both_the_key_and_its_file_are_refused(tmp_path, monkeypatch):
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", KEY)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_FILE", str(key_file))
    with pytest.raises(ValueError, match="both set"):
        Settings(_env_file=None)


@pytest.mark.parametrize("content", [None, ""])
def test_a_missing_or_empty_key_file_is_refused(tmp_path, monkeypatch, content):
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    key_file = tmp_path / "key"
    if content is not None:
        key_file.write_text(content)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_FILE", str(key_file))
    with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY_FILE"):
        Settings(_env_file=None)


def test_changeme_in_the_file_is_refused_like_the_variable(tmp_path, monkeypatch):
    from app.key_strength import is_weak

    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    key_file = tmp_path / "key"
    key_file.write_text("changeme\n")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_FILE", str(key_file))
    settings = Settings(_env_file=None)
    assert settings.credential_encryption_key == "changeme"
    assert is_weak(settings.credential_encryption_key)


# -- 422s in the log --------------------------------------------------------------------


def test_a_rejected_request_is_logged_with_field_names_only(session, app_client):
    with capture_logs() as logs:
        answer = app_client.post("/auth/login", json={"username": "x", "password": 12345})
    assert answer.status_code == 422
    assert "detail" in answer.json()
    [line] = [entry for entry in logs if entry["event"] == "request.validation_failed"]
    assert line["method"] == "POST" and line["path"] == "/auth/login"
    assert line["fields"] == ["body.password"]
    assert "12345" not in repr(line)
    assert line["log_level"] == "info"


@pytest.mark.parametrize("path", ["/auth/login", "/auth/password-reset/complete"])
def test_a_rejected_request_never_echoes_what_was_sent(session, app_client, path):
    secret = "Sent-But-Refused-123"
    answer = app_client.post(path, json={"username": ["x"], "password": [secret], "token": 1})
    assert answer.status_code == 422
    errors = answer.json()["detail"]
    assert errors and all("input" not in error for error in errors)
    assert all({"loc", "msg", "type"} <= error.keys() for error in errors)
    assert secret not in answer.text


def test_a_rejected_reset_request_is_logged_too(session, app_client):
    with capture_logs() as logs:
        answer = app_client.post("/auth/password-reset/check", json={})
    assert answer.status_code == 422
    assert any(entry["event"] == "request.validation_failed" for entry in logs)


# -- downloads --------------------------------------------------------------------------


def test_a_download_is_always_octet_stream(session, tmp_path, monkeypatch):
    from app.api.archive import router as archive_router
    from app.api.auth import get_current_active_user
    from app.api.errors import install as install_error_handlers
    from app.auth.models import Role, User
    from app.db import get_session
    from app.models import PatternKind, Protocol, Rule, RuleType, Source
    from fastapi import FastAPI

    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    get_settings.cache_clear()
    from app.scratch import get_scratch_store

    get_scratch_store.cache_clear()
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "page.html").write_text("<script>alert(1)</script>")
    (folder / "app.log").write_text("line\n")
    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    source = Source(name="s", protocol=Protocol.local, host="localhost", base_path=str(folder))
    session.add(source)
    session.commit()
    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="*",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    app = FastAPI()
    app.include_router(archive_router)
    install_error_handlers(app)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    client = TestClient(app)
    try:
        for name in ("page.html", "app.log"):
            answer = client.get(f"/sources/{source.id}/download", params={"path": name})
            assert answer.status_code == 200, answer.text
            assert answer.headers["content-type"] == "application/octet-stream"
            assert answer.headers["content-disposition"].startswith("attachment")
            assert name in answer.headers["content-disposition"]
    finally:
        get_settings.cache_clear()
        get_scratch_store.cache_clear()
