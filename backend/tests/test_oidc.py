import time

import pytest
from app.audit import record_audit_event  # noqa: F401 - imported for readability of intent
from app.auth.models import AuditLog, AuthProviderType, Role, User
from app.auth.providers import oidc
from app.auth.rbac import create_role
from joserfc import jwt
from joserfc.jwk import KeySet
from sqlmodel import select

ISSUER = "https://idp.example.com"
CLIENT_ID = "perchtail-client"


def _make_key_set():
    return KeySet.generate_key_set("RSA", 2048, count=1)


def _sign_claims(key_set: KeySet, claims: dict) -> str:
    key = key_set.keys[0]
    return jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)


def _valid_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "user-123",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": "the-nonce",
        "email": "jdoe@example.com",
    }
    claims.update(overrides)
    return claims


def _no_access_role(session) -> Role:
    return create_role(session, actor_user_id=None, name="No Access", is_builtin=True)


# --- parse_oidc_settings / build_authorization_url ---------------------------------


def test_parse_oidc_settings_defaults_scopes():
    settings = oidc.parse_oidc_settings(
        {"issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": "s3cret"}
    )
    assert settings.scopes == "openid email profile"


def test_parse_oidc_settings_respects_custom_scopes():
    settings = oidc.parse_oidc_settings(
        {
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "client_secret": "s3cret",
            "scopes": "openid email",
        }
    )
    assert settings.scopes == "openid email"


def test_build_authorization_url_includes_all_required_params():
    url = oidc.build_authorization_url(
        authorization_endpoint=f"{ISSUER}/authorize",
        client_id=CLIENT_ID,
        redirect_uri="https://app.example.com/auth/sso/callback",
        scopes="openid email profile",
        state="the-state",
        nonce="the-nonce",
    )
    assert url.startswith(f"{ISSUER}/authorize?")
    assert "response_type=code" in url
    assert f"client_id={CLIENT_ID}" in url
    assert "state=the-state" in url
    assert "nonce=the-nonce" in url


# --- encode_state / decode_state ---------------------------------------------------


def test_state_roundtrips_to_the_same_nonce():
    state = oidc.encode_state("my-nonce")
    assert oidc.decode_state(state) == "my-nonce"


def test_decode_state_rejects_garbage():
    with pytest.raises(Exception):  # noqa: B017 - Fernet's own InvalidToken
        oidc.decode_state("not-a-real-token")


def test_decode_state_rejects_expired_state(monkeypatch):
    state = oidc.encode_state("my-nonce")
    monkeypatch.setattr(oidc, "STATE_TTL_SECONDS", -1)
    with pytest.raises(Exception):  # noqa: B017
        oidc.decode_state(state)


# --- verify_id_token ----------------------------------------------------------------


def test_verify_id_token_accepts_a_valid_token():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims())
    claims = oidc.verify_id_token(
        token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
    )
    assert claims["sub"] == "user-123"


def test_verify_id_token_rejects_wrong_issuer():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims(iss="https://evil.example.com"))
    with pytest.raises(ValueError):
        oidc.verify_id_token(
            token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
        )


def test_verify_id_token_rejects_wrong_audience():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims(aud="someone-elses-client"))
    with pytest.raises(ValueError, match="audience"):
        oidc.verify_id_token(
            token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
        )


def test_verify_id_token_rejects_nonce_mismatch():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims(nonce="a-different-nonce"))
    with pytest.raises(ValueError, match="nonce"):
        oidc.verify_id_token(
            token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
        )


def test_verify_id_token_rejects_expired_token():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims(exp=int(time.time()) - 60))
    with pytest.raises(ValueError):
        oidc.verify_id_token(
            token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
        )


def test_verify_id_token_rejects_a_token_signed_by_a_different_key():
    signing_key_set = _make_key_set()
    other_key_set = _make_key_set()
    token = _sign_claims(signing_key_set, _valid_claims())
    with pytest.raises(Exception):  # noqa: B017 - joserfc's own signature-verification error
        oidc.verify_id_token(
            token,
            jwks=other_key_set.as_dict(),
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="the-nonce",
        )


def test_verify_id_token_accepts_aud_as_a_list():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims(aud=["some-other-client", CLIENT_ID]))
    claims = oidc.verify_id_token(
        token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
    )
    assert claims["sub"] == "user-123"


# --- find_or_provision_user ---------------------------------------------------------


def test_find_or_provision_user_creates_a_new_user_with_no_access_role(session):
    role = _no_access_role(session)

    user = oidc.find_or_provision_user(
        session, external_id="sub-1", email="new@example.com", no_access_role_id=role.id
    )

    assert user.username == "new@example.com"
    assert user.role_id == role.id
    assert user.auth_provider == AuthProviderType.oidc
    assert user.external_id == "sub-1"
    assert user.password_hash is None

    actions = [e.action for e in session.exec(select(AuditLog)).all()]
    assert "user.sso_provision" in actions


def test_find_or_provision_user_reuses_the_existing_user_on_a_second_login(session):
    role = _no_access_role(session)
    first = oidc.find_or_provision_user(
        session, external_id="sub-1", email="new@example.com", no_access_role_id=role.id
    )

    second = oidc.find_or_provision_user(
        session, external_id="sub-1", email="new@example.com", no_access_role_id=role.id
    )

    assert second.id == first.id
    assert session.exec(select(User)).all() == [first]


# --- OIDCProvider.complete_login (orchestration, with discovery/token/jwks mocked) --


@pytest.fixture()
def discovery_document():
    return {
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }


def _patch_idp(monkeypatch, *, discovery, tokens, jwks):
    monkeypatch.setattr(oidc, "fetch_discovery_document", lambda issuer: discovery)
    monkeypatch.setattr(
        oidc,
        "exchange_code_for_tokens",
        lambda **kwargs: tokens,
    )
    monkeypatch.setattr(oidc, "fetch_jwks", lambda jwks_uri: jwks)


def test_complete_login_provisions_and_signs_in_a_new_user(
    session, monkeypatch, discovery_document
):
    role = _no_access_role(session)
    key_set = _make_key_set()
    id_token = _sign_claims(key_set, _valid_claims())
    _patch_idp(
        monkeypatch,
        discovery=discovery_document,
        tokens={"id_token": id_token},
        jwks=key_set.as_dict(),
    )

    settings = oidc.OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret")
    user = oidc.OIDCProvider(settings).complete_login(
        session,
        code="the-code",
        redirect_uri="https://app.example.com/auth/sso/callback",
        nonce="the-nonce",
        no_access_role_id=role.id,
    )

    assert user.username == "jdoe@example.com"
    assert user.role_id == role.id
    assert user.last_login_at is not None
    actions = [e.action for e in session.exec(select(AuditLog)).all()]
    assert "user.login" in actions


def test_complete_login_raises_when_idp_omits_id_token(session, monkeypatch, discovery_document):
    role = _no_access_role(session)
    _patch_idp(monkeypatch, discovery=discovery_document, tokens={}, jwks={"keys": []})

    settings = oidc.OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret")
    with pytest.raises(ValueError, match="id_token"):
        oidc.OIDCProvider(settings).complete_login(
            session,
            code="the-code",
            redirect_uri="https://app.example.com/auth/sso/callback",
            nonce="the-nonce",
            no_access_role_id=role.id,
        )


def test_complete_login_rejects_a_deactivated_account(session, monkeypatch, discovery_document):
    role = _no_access_role(session)
    # Pre-existing, deactivated SSO user
    existing = oidc.find_or_provision_user(
        session, external_id="user-123", email="jdoe@example.com", no_access_role_id=role.id
    )
    existing.active = False
    session.add(existing)
    session.commit()

    key_set = _make_key_set()
    id_token = _sign_claims(key_set, _valid_claims())
    _patch_idp(
        monkeypatch,
        discovery=discovery_document,
        tokens={"id_token": id_token},
        jwks=key_set.as_dict(),
    )

    settings = oidc.OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret")
    with pytest.raises(ValueError, match="deactivated"):
        oidc.OIDCProvider(settings).complete_login(
            session,
            code="the-code",
            redirect_uri="https://app.example.com/auth/sso/callback",
            nonce="the-nonce",
            no_access_role_id=role.id,
        )
