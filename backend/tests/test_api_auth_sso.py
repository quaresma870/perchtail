import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from app.api.auth import router as auth_router
from app.auth.models import AuthProviderType, Role, SSOProtocol, SSOProviderConfig, User
from app.auth.providers import oidc
from app.auth.providers.local import create_local_user
from app.bootstrap import seed_no_access_role
from app.config import get_settings
from app.crypto import encrypt_secret
from app.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import KeySet
from sqlmodel import select

ISSUER = "https://idp.example.com"
CLIENT_ID = "perchtail-client"
DISCOVERY = {
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
}


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app, follow_redirects=False)

    get_settings.cache_clear()


def _make_enabled_provider(session, **config_overrides) -> SSOProviderConfig:
    config = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "s3cret",
        "scopes": "openid email profile",
        **config_overrides,
    }
    provider = SSOProviderConfig(
        protocol=SSOProtocol.oidc,
        name="Corporate SSO",
        config=encrypt_secret(json.dumps(config)),
        enabled=True,
    )
    session.add(provider)
    session.commit()
    session.refresh(provider)
    return provider


def _key_set_and_token(**claim_overrides) -> tuple[KeySet, str]:
    key_set = KeySet.generate_key_set("RSA", 2048, count=1)
    key = key_set.keys[0]
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "user-123",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": "placeholder",
        "email": "jdoe@example.com",
    }
    claims.update(claim_overrides)
    token = jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)
    return key_set, token


# --- /auth/sso/status ---------------------------------------------------------------


def test_sso_status_reports_disabled_when_nothing_configured(client):
    response = client.get("/auth/sso/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "name": None}


def test_sso_status_reports_enabled_provider_name(client, session):
    _make_enabled_provider(session)
    response = client.get("/auth/sso/status")
    assert response.json() == {"enabled": True, "name": "Corporate SSO"}


def test_sso_status_ignores_a_disabled_provider(client, session):
    provider = _make_enabled_provider(session)
    provider.enabled = False
    session.add(provider)
    session.commit()

    response = client.get("/auth/sso/status")
    assert response.json()["enabled"] is False


# --- /auth/sso/login -----------------------------------------------------------------


def test_sso_login_404s_when_nothing_is_enabled(client):
    response = client.get("/auth/sso/login")
    assert response.status_code == 404


def test_sso_login_redirects_to_the_idp_authorization_endpoint(client, session, monkeypatch):
    _make_enabled_provider(session)
    monkeypatch.setattr(oidc, "fetch_discovery_document", lambda issuer: DISCOVERY)

    response = client.get("/auth/sso/login")

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/authorize?")
    params = parse_qs(urlparse(location).query)
    assert params["client_id"] == [CLIENT_ID]
    assert params["response_type"] == ["code"]
    assert "state" in params
    assert "nonce" in params


def test_sso_login_returns_502_when_idp_is_unreachable(client, session, monkeypatch):
    _make_enabled_provider(session)

    def _boom(issuer):
        raise ConnectionError("timed out")

    monkeypatch.setattr(oidc, "fetch_discovery_document", _boom)

    response = client.get("/auth/sso/login")
    assert response.status_code == 502


# --- /auth/sso/callback --------------------------------------------------------------


def _patch_token_and_jwks(monkeypatch, *, key_set, id_token):
    monkeypatch.setattr(oidc, "fetch_discovery_document", lambda issuer: DISCOVERY)
    monkeypatch.setattr(oidc, "exchange_code_for_tokens", lambda **kwargs: {"id_token": id_token})
    monkeypatch.setattr(oidc, "fetch_jwks", lambda jwks_uri: key_set.as_dict())


def _get_state_and_nonce(client, session, monkeypatch):
    """Drives a real /auth/sso/login call to get a state param whose nonce
    this test controls, mirroring exactly what a browser would round-trip
    back on the callback."""
    monkeypatch.setattr(oidc, "fetch_discovery_document", lambda issuer: DISCOVERY)
    login_response = client.get("/auth/sso/login")
    location = login_response.headers["location"]
    params = parse_qs(urlparse(location).query)
    return params["state"][0], params["nonce"][0]


def test_callback_provisions_a_new_user_and_sets_a_session_cookie(client, session, monkeypatch):
    seed_no_access_role(session)
    _make_enabled_provider(session)
    state, nonce = _get_state_and_nonce(client, session, monkeypatch)
    key_set, id_token = _key_set_and_token(nonce=nonce)
    _patch_token_and_jwks(monkeypatch, key_set=key_set, id_token=id_token)

    response = client.get("/auth/sso/callback", params={"code": "the-code", "state": state})

    assert response.status_code == 302
    assert response.headers["location"] == f"{get_settings().public_base_url}/"
    assert "perchtail_session" in response.cookies

    user = session.exec(select(User).where(User.auth_provider == AuthProviderType.oidc)).first()
    assert user is not None
    assert user.username == "jdoe@example.com"
    no_access = session.exec(select(Role).where(Role.name == "No Access")).first()
    assert user.role_id == no_access.id


def test_callback_reuses_the_same_user_on_a_second_login(client, session, monkeypatch):
    seed_no_access_role(session)
    _make_enabled_provider(session)

    state, nonce = _get_state_and_nonce(client, session, monkeypatch)
    key_set, id_token = _key_set_and_token(nonce=nonce)
    _patch_token_and_jwks(monkeypatch, key_set=key_set, id_token=id_token)
    client.get("/auth/sso/callback", params={"code": "the-code", "state": state})

    state2, nonce2 = _get_state_and_nonce(client, session, monkeypatch)
    key_set2, id_token2 = _key_set_and_token(nonce=nonce2)
    _patch_token_and_jwks(monkeypatch, key_set=key_set2, id_token=id_token2)
    client.get("/auth/sso/callback", params={"code": "another-code", "state": state2})

    users = session.exec(select(User).where(User.auth_provider == AuthProviderType.oidc)).all()
    assert len(users) == 1


def test_callback_redirects_to_login_error_on_idp_error_param(client, session):
    response = client.get(
        "/auth/sso/callback", params={"error": "access_denied", "state": "irrelevant"}
    )
    assert response.status_code == 302
    assert "sso_error=1" in response.headers["location"]
    assert "perchtail_session" not in response.cookies


def test_callback_redirects_to_login_error_on_missing_code(client, session):
    response = client.get("/auth/sso/callback", params={"state": "irrelevant"})
    assert response.status_code == 302
    assert "sso_error=1" in response.headers["location"]


def test_callback_redirects_to_login_error_on_invalid_state(client, session):
    seed_no_access_role(session)
    _make_enabled_provider(session)

    response = client.get(
        "/auth/sso/callback", params={"code": "the-code", "state": "not-a-real-state"}
    )
    assert response.status_code == 302
    assert "sso_error=1" in response.headers["location"]
    assert "perchtail_session" not in response.cookies


def test_callback_redirects_to_login_error_when_nonce_does_not_match(client, session, monkeypatch):
    seed_no_access_role(session)
    _make_enabled_provider(session)
    state, _correct_nonce = _get_state_and_nonce(client, session, monkeypatch)
    # ID token signed with a nonce that doesn't match what's embedded in state
    key_set, id_token = _key_set_and_token(nonce="a-forged-nonce")
    _patch_token_and_jwks(monkeypatch, key_set=key_set, id_token=id_token)

    response = client.get("/auth/sso/callback", params={"code": "the-code", "state": state})

    assert response.status_code == 302
    assert "sso_error=1" in response.headers["location"]
    assert "perchtail_session" not in response.cookies


def test_callback_redirects_to_login_error_for_a_deactivated_account(client, session, monkeypatch):
    role = seed_no_access_role(session)
    _make_enabled_provider(session)

    existing = User(
        username="jdoe@example.com",
        role_id=role.id,
        auth_provider=AuthProviderType.oidc,
        external_id="user-123",
        active=False,
    )
    session.add(existing)
    session.commit()

    state, nonce = _get_state_and_nonce(client, session, monkeypatch)
    key_set, id_token = _key_set_and_token(nonce=nonce)
    _patch_token_and_jwks(monkeypatch, key_set=key_set, id_token=id_token)

    response = client.get("/auth/sso/callback", params={"code": "the-code", "state": state})

    assert response.status_code == 302
    assert "sso_error=1" in response.headers["location"]
    assert "perchtail_session" not in response.cookies


def test_callback_redirects_to_login_error_when_sso_disabled_mid_flow(client, session, monkeypatch):
    seed_no_access_role(session)
    provider = _make_enabled_provider(session)
    state, nonce = _get_state_and_nonce(client, session, monkeypatch)

    provider.enabled = False
    session.add(provider)
    session.commit()

    key_set, id_token = _key_set_and_token(nonce=nonce)
    _patch_token_and_jwks(monkeypatch, key_set=key_set, id_token=id_token)

    response = client.get("/auth/sso/callback", params={"code": "the-code", "state": state})
    assert response.status_code == 302
    assert "sso_error=1" in response.headers["location"]


# --- local auth keeps working alongside SSO (Phase 1b checklist item) ---------------


def test_local_login_still_works_with_an_sso_provider_enabled(client, session):
    """CLAUDE.md: "SSO always coexists with local auth — never remove the
    local break-glass account." An enabled SSOProviderConfig must not
    change local /auth/login behavior at all."""
    _make_enabled_provider(session)
    role = seed_no_access_role(session)
    create_local_user(
        session,
        actor_user_id=None,
        username="admin@example.com",
        password="s3cret!",
        role_id=role.id,
    )

    response = client.post(
        "/auth/login", json={"username": "admin@example.com", "password": "s3cret!"}
    )

    assert response.status_code == 200
    assert response.json()["username"] == "admin@example.com"
    assert "perchtail_session" in response.cookies
