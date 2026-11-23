import time
from urllib.parse import parse_qs, urlsplit

import pytest
from app.audit import record_audit_event  # noqa: F401 - imported for readability of intent
from app.auth.models import AuditLog, AuthProviderType, Role, SSOGroupRoleMapping, User
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
        "email_verified": True,
    }
    claims.update(overrides)
    return claims


class _FakeResponse:
    def __init__(self, document):
        self.document = document

    def json(self):
        return self.document


def _serve(monkeypatch, document, sent: list | None = None):
    """Answers every request to the IdP with `document`, recording
    (method, url, kwargs) in `sent`."""

    def fake_request(method, url, what, **kwargs):
        oidc.require_https(url, what)
        if sent is not None:
            sent.append((method, url, kwargs))
        return _FakeResponse(document)

    monkeypatch.setattr(oidc, "_idp_request", fake_request)


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
        code_challenge="the-challenge",
    )
    assert url.startswith(f"{ISSUER}/authorize?")
    assert "response_type=code" in url
    assert f"client_id={CLIENT_ID}" in url
    assert "state=the-state" in url
    assert "nonce=the-nonce" in url
    assert "code_challenge=the-challenge" in url
    assert "code_challenge_method=S256" in url


def test_the_code_challenge_is_the_s256_of_the_verifier():
    # RFC 7636, appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oidc.code_challenge_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_code_verifiers_are_random_and_within_the_rfc_length():
    first, second = oidc.generate_code_verifier(), oidc.generate_code_verifier()
    assert first != second
    assert 43 <= len(first) <= 128


def test_the_authorization_redirect_sends_the_s256_challenge(monkeypatch, discovery_document):
    monkeypatch.setattr(oidc, "fetch_discovery_document", lambda issuer: discovery_document)
    settings = oidc.OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret")

    url = oidc.OIDCProvider(settings).authorization_redirect_url(
        redirect_uri="https://app.example.com/auth/sso/callback",
        state="s",
        nonce="n",
        code_verifier="the-verifier",
    )

    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge"] == [oidc.code_challenge_s256("the-verifier")]
    assert query["code_challenge_method"] == ["S256"]
    assert "the-verifier" not in url


def test_the_code_exchange_sends_the_verifier(monkeypatch):
    requests = []
    _serve(monkeypatch, {"id_token": "t"}, requests)
    oidc.exchange_code_for_tokens(
        token_endpoint=f"{ISSUER}/token",
        code="c",
        redirect_uri="https://app.example.com/auth/sso/callback",
        client_id=CLIENT_ID,
        client_secret="s3cret",
        code_verifier="the-verifier",
    )
    [(method, _url, kwargs)] = requests
    assert method == "POST"
    assert kwargs["data"]["code_verifier"] == "the-verifier"


# --- encode_state / decode_state ---------------------------------------------------


def test_state_roundtrips_to_the_same_nonce_and_verifier():
    state = oidc.encode_state("my-nonce", "my-verifier")
    assert oidc.decode_state(state) == ("my-nonce", "my-verifier")
    assert "my-verifier" not in state


def test_decode_state_rejects_garbage():
    with pytest.raises(Exception):  # noqa: B017 - Fernet's own InvalidToken
        oidc.decode_state("not-a-real-token")


def test_decode_state_rejects_a_state_from_before_pkce():
    # A login started before the upgrade carried only the nonce: start over.
    from app.crypto import encrypt_secret

    with pytest.raises(Exception):  # noqa: B017
        oidc.decode_state(encrypt_secret("my-nonce"))


def test_decode_state_rejects_expired_state(monkeypatch):
    state = oidc.encode_state("my-nonce", "my-verifier")
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


@pytest.mark.parametrize("claim", ["exp", "iat"])
def test_verify_id_token_rejects_a_token_without_exp_or_iat(claim):
    key_set = _make_key_set()
    claims = _valid_claims()
    del claims[claim]
    token = _sign_claims(key_set, claims)
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
    token = _sign_claims(
        key_set, _valid_claims(aud=["some-other-client", CLIENT_ID], azp=CLIENT_ID)
    )
    claims = oidc.verify_id_token(
        token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
    )
    assert claims["sub"] == "user-123"


def test_verify_id_token_accepts_a_single_audience_in_a_list_without_azp():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims(aud=[CLIENT_ID]))
    oidc.verify_id_token(
        token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
    )


@pytest.mark.parametrize("azp", [None, "some-other-client", ""])
def test_verify_id_token_with_several_audiences_needs_azp_naming_this_client(azp):
    key_set = _make_key_set()
    claims = _valid_claims(aud=["some-other-client", CLIENT_ID])
    if azp is not None:
        claims["azp"] = azp
    token = _sign_claims(key_set, claims)
    with pytest.raises(ValueError, match="authorized party"):
        oidc.verify_id_token(
            token, jwks=key_set.as_dict(), issuer=ISSUER, client_id=CLIENT_ID, nonce="the-nonce"
        )


# --- signature algorithms --------------------------------------------------------------


def _unsigned_token(claims: dict) -> str:
    import base64
    import json

    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}."


def test_verify_id_token_refuses_an_unsigned_token():
    key_set = _make_key_set()
    with pytest.raises(Exception):  # noqa: B017 - joserfc's own error
        oidc.verify_id_token(
            _unsigned_token(_valid_claims()),
            jwks=key_set.as_dict(),
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="the-nonce",
            algorithms=["none", "RS256"],
        )


def test_verify_id_token_refuses_an_hmac_token_even_when_announced():
    from joserfc.jwk import OctKey

    key = OctKey.generate_key(256)
    token = jwt.encode({"alg": "HS256", "kid": "k"}, _valid_claims(), key)
    jwks = {"keys": [{**key.as_dict(private=True), "kid": "k"}]}
    with pytest.raises(Exception):  # noqa: B017 - joserfc's own error or ValueError
        oidc.verify_id_token(
            token,
            jwks=jwks,
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="the-nonce",
            algorithms=["HS256", "RS256"],
        )


def test_verify_id_token_refuses_an_algorithm_the_provider_does_not_announce():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims())
    with pytest.raises(Exception):  # noqa: B017 - joserfc's own error
        oidc.verify_id_token(
            token,
            jwks=key_set.as_dict(),
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="the-nonce",
            algorithms=["ES256"],
        )


def test_verify_id_token_with_no_asymmetric_algorithm_refuses_everything():
    key_set = _make_key_set()
    token = _sign_claims(key_set, _valid_claims())
    with pytest.raises(ValueError, match="algorithm"):
        oidc.verify_id_token(
            token,
            jwks=key_set.as_dict(),
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="the-nonce",
            algorithms=["none", "HS256"],
        )


@pytest.mark.parametrize(
    ("announced", "expected"),
    [
        (["RS256", "HS256", "none", "ES256"], ["RS256", "ES256"]),
        (["HS256", "none"], []),
        (None, ["RS256"]),
        ([], ["RS256"]),
        ("RS256", ["RS256"]),
    ],
)
def test_id_token_algorithms_keeps_only_the_announced_asymmetric_ones(announced, expected):
    discovery = {} if announced is None else {"id_token_signing_alg_values_supported": announced}
    assert oidc.id_token_algorithms(discovery) == expected


# --- find_or_provision_user ---------------------------------------------------------


def test_find_or_provision_user_creates_a_new_user_with_no_access_role(session):
    role = _no_access_role(session)

    user = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="sub-1",
        email="new@example.com",
        no_access_role_id=role.id,
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
        session,
        issuer=ISSUER,
        external_id="sub-1",
        email="new@example.com",
        no_access_role_id=role.id,
    )

    second = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="sub-1",
        email="new@example.com",
        no_access_role_id=role.id,
    )

    assert second.id == first.id
    assert session.exec(select(User)).all() == [first]


# --- OIDCProvider.complete_login (orchestration, with discovery/token/jwks mocked) --


@pytest.fixture()
def discovery_document():
    return {
        "issuer": ISSUER,
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
        code_verifier="the-verifier",
        no_access_role_id=role.id,
    )

    assert user.username == "jdoe@example.com"
    assert user.external_issuer == ISSUER
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
            code_verifier="the-verifier",
            no_access_role_id=role.id,
        )


def test_complete_login_rejects_a_deactivated_account(session, monkeypatch, discovery_document):
    role = _no_access_role(session)
    # Pre-existing, deactivated SSO user
    existing = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="user-123",
        email="jdoe@example.com",
        no_access_role_id=role.id,
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
            code_verifier="the-verifier",
            no_access_role_id=role.id,
        )


# --- extract_claim_groups / resolve_group_mapped_role_id ---------------------------


def test_extract_claim_groups_returns_empty_when_group_claim_not_configured():
    assert oidc.extract_claim_groups({"groups": ["a"]}, group_claim=None) == []


def test_extract_claim_groups_returns_empty_when_claim_missing():
    assert oidc.extract_claim_groups({}, group_claim="groups") == []


def test_extract_claim_groups_normalizes_a_bare_string_to_a_list():
    assert oidc.extract_claim_groups({"groups": "engineering"}, group_claim="groups") == [
        "engineering"
    ]


def test_extract_claim_groups_passes_through_a_list():
    claims = {"groups": ["engineering", "on-call"]}
    assert oidc.extract_claim_groups(claims, group_claim="groups") == ["engineering", "on-call"]


def test_resolve_group_mapped_role_id_returns_none_when_no_groups(session):
    assert oidc.resolve_group_mapped_role_id(session, []) is None


def test_resolve_group_mapped_role_id_returns_none_when_nothing_matches(session):
    role = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=0, group_name="engineering", role_id=role.id))
    session.commit()

    assert oidc.resolve_group_mapped_role_id(session, ["sales"]) is None


def test_resolve_group_mapped_role_id_returns_the_matching_role(session):
    role = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=0, group_name="engineering", role_id=role.id))
    session.commit()

    assert oidc.resolve_group_mapped_role_id(session, ["engineering"]) == role.id


def test_resolve_group_mapped_role_id_last_match_wins(session):
    tier1 = create_role(session, actor_user_id=None, name="Tier1")
    tier2 = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=0, group_name="staff", role_id=tier1.id))
    session.add(SSOGroupRoleMapping(order=1, group_name="engineering", role_id=tier2.id))
    session.commit()

    # Both mappings match this user's groups; the later-ordered one wins,
    # same "last match wins" semantics as Rule.
    assert oidc.resolve_group_mapped_role_id(session, ["staff", "engineering"]) == tier2.id


def test_resolve_group_mapped_role_id_respects_order_not_insertion_order(session):
    tier1 = create_role(session, actor_user_id=None, name="Tier1")
    tier2 = create_role(session, actor_user_id=None, name="Tier2")
    # Inserted out of `order` sequence on purpose.
    session.add(SSOGroupRoleMapping(order=5, group_name="engineering", role_id=tier2.id))
    session.add(SSOGroupRoleMapping(order=1, group_name="staff", role_id=tier1.id))
    session.commit()

    assert oidc.resolve_group_mapped_role_id(session, ["staff", "engineering"]) == tier2.id


def test_find_or_provision_user_does_not_match_the_same_sub_from_another_issuer(session):
    role = _no_access_role(session)
    first = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="sub-1",
        email="a@example.com",
        no_access_role_id=role.id,
    )

    other = oidc.find_or_provision_user(
        session,
        issuer="https://other-idp.example.com",
        external_id="sub-1",
        email="b@example.com",
        no_access_role_id=role.id,
    )

    assert other.id != first.id
    assert other.external_issuer == "https://other-idp.example.com"
    assert first.external_issuer == ISSUER


def test_find_or_provision_user_ignores_an_account_not_yet_bound_to_an_issuer(session):
    """An account from before issuers were recorded is only bound at
    startup (bootstrap.backfill_sso_user_issuers), never claimed by whichever
    IdP logs in first."""
    role = _no_access_role(session)
    legacy = User(
        username="legacy@example.com",
        role_id=role.id,
        auth_provider=AuthProviderType.oidc,
        external_id="sub-1",
    )
    session.add(legacy)
    session.commit()

    user = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="sub-1",
        email="new@example.com",
        no_access_role_id=role.id,
    )

    assert user.id != legacy.id


# --- find_or_provision_user with group mapping --------------------------------------


def test_find_or_provision_user_uses_mapped_role_for_a_new_user(session):
    no_access = _no_access_role(session)
    mapped = create_role(session, actor_user_id=None, name="Tier2")

    user = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="sub-1",
        email="new@example.com",
        no_access_role_id=no_access.id,
        mapped_role_id=mapped.id,
    )

    assert user.role_id == mapped.id


def test_find_or_provision_user_falls_back_to_no_access_without_a_mapped_role(session):
    no_access = _no_access_role(session)

    user = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="sub-1",
        email="new@example.com",
        no_access_role_id=no_access.id,
        mapped_role_id=None,
    )

    assert user.role_id == no_access.id


# --- OIDCProvider.complete_login group sync -----------------------------------------


def test_complete_login_provisions_a_new_user_directly_into_the_mapped_role(
    session, monkeypatch, discovery_document
):
    no_access = _no_access_role(session)
    mapped = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=0, group_name="engineering", role_id=mapped.id))
    session.commit()

    key_set = _make_key_set()
    id_token = _sign_claims(key_set, _valid_claims(groups=["engineering"]))
    _patch_idp(
        monkeypatch,
        discovery=discovery_document,
        tokens={"id_token": id_token},
        jwks=key_set.as_dict(),
    )

    settings = oidc.OIDCSettings(
        issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret", group_claim="groups"
    )
    user = oidc.OIDCProvider(settings).complete_login(
        session,
        code="the-code",
        redirect_uri="https://app.example.com/auth/sso/callback",
        nonce="the-nonce",
        code_verifier="the-verifier",
        no_access_role_id=no_access.id,
    )

    assert user.role_id == mapped.id


def test_complete_login_resyncs_an_existing_users_role_on_each_login(
    session, monkeypatch, discovery_document
):
    no_access = _no_access_role(session)
    existing = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="user-123",
        email="jdoe@example.com",
        no_access_role_id=no_access.id,
    )
    assert existing.role_id == no_access.id

    mapped = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=0, group_name="engineering", role_id=mapped.id))
    session.commit()

    key_set = _make_key_set()
    id_token = _sign_claims(key_set, _valid_claims(groups=["engineering"]))
    _patch_idp(
        monkeypatch,
        discovery=discovery_document,
        tokens={"id_token": id_token},
        jwks=key_set.as_dict(),
    )

    settings = oidc.OIDCSettings(
        issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret", group_claim="groups"
    )
    user = oidc.OIDCProvider(settings).complete_login(
        session,
        code="the-code",
        redirect_uri="https://app.example.com/auth/sso/callback",
        nonce="the-nonce",
        code_verifier="the-verifier",
        no_access_role_id=no_access.id,
    )

    assert user.id == existing.id
    assert user.role_id == mapped.id
    actions = [e.action for e in session.exec(select(AuditLog)).all()]
    assert "user.sso_role_synced" in actions


def test_complete_login_drops_to_no_access_when_no_group_matches(
    session, monkeypatch, discovery_document
):
    no_access = _no_access_role(session)
    manually_promoted = create_role(session, actor_user_id=None, name="ManuallyPromoted")
    existing = oidc.find_or_provision_user(
        session,
        issuer=ISSUER,
        external_id="user-123",
        email="jdoe@example.com",
        no_access_role_id=no_access.id,
    )
    existing.role_id = manually_promoted.id
    session.add(existing)
    session.commit()

    other_role = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=0, group_name="engineering", role_id=other_role.id))
    session.commit()

    key_set = _make_key_set()
    # This user's groups claim doesn't include "engineering": no mapping
    # applies, so the manually-set role goes and the account drops to the
    # no-access role.
    id_token = _sign_claims(key_set, _valid_claims(groups=["sales"]))
    _patch_idp(
        monkeypatch,
        discovery=discovery_document,
        tokens={"id_token": id_token},
        jwks=key_set.as_dict(),
    )

    settings = oidc.OIDCSettings(
        issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret", group_claim="groups"
    )
    user = oidc.OIDCProvider(settings).complete_login(
        session,
        code="the-code",
        redirect_uri="https://app.example.com/auth/sso/callback",
        nonce="the-nonce",
        code_verifier="the-verifier",
        no_access_role_id=no_access.id,
    )

    assert user.role_id == no_access.id
    assert user.sso_role_group is None
    [synced] = session.exec(select(AuditLog).where(AuditLog.action == "user.sso_role_synced")).all()
    assert synced.event_metadata["role_id"] == no_access.id
    assert synced.event_metadata["reason"] == "no_matching_mapping"


def _login_with_groups(session, monkeypatch, discovery_document, no_access, groups):
    key_set = _make_key_set()
    id_token = _sign_claims(key_set, _valid_claims(groups=groups))
    _patch_idp(
        monkeypatch,
        discovery=discovery_document,
        tokens={"id_token": id_token},
        jwks=key_set.as_dict(),
    )
    settings = oidc.OIDCSettings(
        issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret", group_claim="groups"
    )
    return oidc.OIDCProvider(settings).complete_login(
        session,
        code="the-code",
        redirect_uri="https://app.example.com/auth/sso/callback",
        nonce="the-nonce",
        code_verifier="the-verifier",
        no_access_role_id=no_access.id,
    )


def test_complete_login_records_only_the_group_whose_mapping_gave_the_role(
    session, monkeypatch, discovery_document
):
    no_access = _no_access_role(session)
    tier1 = create_role(session, actor_user_id=None, name="Tier1")
    tier2 = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=0, group_name="staff", role_id=tier1.id))
    session.add(SSOGroupRoleMapping(order=1, group_name="engineering", role_id=tier2.id))
    session.commit()

    user = _login_with_groups(
        session, monkeypatch, discovery_document, no_access, ["staff", "engineering", "vpn"]
    )

    assert user.role_id == tier2.id
    # Last match wins, and the token's other groups aren't kept.
    assert user.sso_role_group == "engineering"


def test_complete_login_clears_the_sso_role_group_when_no_mapping_matches(
    session, monkeypatch, discovery_document
):
    no_access = _no_access_role(session)
    tier2 = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=0, group_name="engineering", role_id=tier2.id))
    session.commit()
    user = _login_with_groups(session, monkeypatch, discovery_document, no_access, ["engineering"])
    assert user.sso_role_group == "engineering"

    user = _login_with_groups(session, monkeypatch, discovery_document, no_access, ["sales"])

    # Out of the group: the role it gave is gone.
    assert user.role_id == no_access.id
    assert user.sso_role_group is None


def _super_admin_role(session, name="SSO admins") -> Role:
    role = Role(name=name, is_super_admin=True)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def test_leaving_the_group_takes_away_a_super_admin_role_when_another_remains(
    session, monkeypatch, discovery_document
):
    no_access = _no_access_role(session)
    admins = _super_admin_role(session)
    session.add(SSOGroupRoleMapping(order=0, group_name="admins", role_id=admins.id))
    session.add(User(username="break-glass", role_id=admins.id))
    session.commit()
    user = _login_with_groups(session, monkeypatch, discovery_document, no_access, ["admins"])
    assert user.role_id == admins.id

    user = _login_with_groups(session, monkeypatch, discovery_document, no_access, [])

    assert user.role_id == no_access.id


def test_the_last_active_super_admin_keeps_the_role_and_a_warning_is_logged(
    session, monkeypatch, discovery_document
):
    from structlog.testing import capture_logs

    no_access = _no_access_role(session)
    admins = _super_admin_role(session)
    tier1 = create_role(session, actor_user_id=None, name="Tier1")
    session.add(SSOGroupRoleMapping(order=0, group_name="admins", role_id=admins.id))
    session.add(SSOGroupRoleMapping(order=1, group_name="staff", role_id=tier1.id))
    # Another super admin, but deactivated: doesn't count.
    session.add(User(username="former", role_id=admins.id, active=False))
    session.commit()
    user = _login_with_groups(session, monkeypatch, discovery_document, no_access, ["admins"])
    assert user.role_id == admins.id
    assert user.sso_role_group == "admins"

    for groups in ([], ["staff"]):
        with capture_logs() as logs:
            user = _login_with_groups(session, monkeypatch, discovery_document, no_access, groups)
        assert user.role_id == admins.id
        assert any(e["event"] == "sso.role_sync_kept_last_super_admin" for e in logs)
        # The kept role wasn't given by the group that matched: the access
        # report mustn't name "staff" as where Super Admin came from.
        assert user.sso_role_group is None


def test_the_username_comes_from_email_only_when_it_is_verified(
    session, monkeypatch, discovery_document
):
    no_access = _no_access_role(session)
    settings = oidc.OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret")
    cases = [
        ("sub-1", {"email_verified": True}, "jdoe@example.com"),
        ("sub-2", {"email_verified": "true"}, "jdoe2@example.com"),
        ("sub-3", {"email_verified": False, "preferred_username": "jd3"}, "jd3"),
        ("sub-4", {"email_verified": "false"}, "sub-4"),
        ("sub-5", {"preferred_username": "jd5"}, "jd5"),
    ]
    for sub, extra, expected in cases:
        claims = _valid_claims(sub=sub, email=f"{expected}" if "@" in expected else "x@e.com")
        claims.pop("email_verified")
        claims.update(extra)
        key_set = _make_key_set()
        _patch_idp(
            monkeypatch,
            discovery=discovery_document,
            tokens={"id_token": _sign_claims(key_set, claims)},
            jwks=key_set.as_dict(),
        )
        user = oidc.OIDCProvider(settings).complete_login(
            session,
            code="c",
            redirect_uri="https://app.example.com/auth/sso/callback",
            nonce="the-nonce",
            code_verifier="v",
            no_access_role_id=no_access.id,
        )
        assert user.username == expected


def test_complete_login_uses_only_the_algorithms_the_provider_announces(
    session, monkeypatch, discovery_document
):
    no_access = _no_access_role(session)
    key_set = _make_key_set()
    _patch_idp(
        monkeypatch,
        discovery={**discovery_document, "id_token_signing_alg_values_supported": ["ES256"]},
        tokens={"id_token": _sign_claims(key_set, _valid_claims())},
        jwks=key_set.as_dict(),
    )
    settings = oidc.OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret")
    with pytest.raises(Exception):  # noqa: B017 - joserfc's own error
        oidc.OIDCProvider(settings).complete_login(
            session,
            code="c",
            redirect_uri="https://app.example.com/auth/sso/callback",
            nonce="the-nonce",
            code_verifier="v",
            no_access_role_id=no_access.id,
        )


def test_resolve_group_mapping_returns_the_last_matching_mapping(session):
    tier1 = create_role(session, actor_user_id=None, name="Tier1")
    tier2 = create_role(session, actor_user_id=None, name="Tier2")
    session.add(SSOGroupRoleMapping(order=1, group_name="engineering", role_id=tier2.id))
    session.add(SSOGroupRoleMapping(order=0, group_name="staff", role_id=tier1.id))
    session.commit()

    mapping = oidc.resolve_group_mapping(session, ["staff", "engineering"])

    assert mapping is not None
    assert (mapping.group_name, mapping.role_id) == ("engineering", tier2.id)
    assert oidc.resolve_group_mapping(session, ["sales"]) is None


# --- the IdP over HTTPS only ----------------------------------------------------------


@pytest.fixture()
def http_localhost_allowed(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "sso_allow_http_localhost", True)


@pytest.mark.parametrize(
    "url",
    [
        "http://idp.example.com",
        "http://localhost:8080/realms/x",
        "ftp://idp.example.com",
        "https://",
        "idp.example.com",
        "",
    ],
)
def test_require_https_refuses_anything_but_https(url):
    with pytest.raises(oidc.InsecureIdPURLError, match="must be an https:// URL"):
        oidc.require_https(url, "issuer URL")


def test_require_https_accepts_https():
    oidc.require_https("https://idp.example.com/realms/x", "issuer URL")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_http_on_loopback_only_with_the_development_switch(http_localhost_allowed, host):
    oidc.require_https(f"http://{host}:8080/realms/x", "issuer URL")


def test_the_development_switch_never_allows_another_host(http_localhost_allowed):
    with pytest.raises(oidc.InsecureIdPURLError):
        oidc.require_https("http://idp.example.com", "issuer URL")
    with pytest.raises(oidc.InsecureIdPURLError):
        oidc.require_https("http://localhost.evil.example", "issuer URL")


class _RecordingClient:
    """Stands in for httpx.Client: records each request and answers with
    `status` and `document`."""

    calls: list = []
    status = 200
    document: dict = {}
    headers: dict = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        type(self).calls.append({"method": method, "url": url, "client": self.kwargs, **kwargs})
        return oidc.httpx.Response(
            self.status,
            json=self.document,
            headers=self.headers,
            request=oidc.httpx.Request(method, url),
        )


@pytest.fixture()
def idp_client(monkeypatch):
    class Client(_RecordingClient):
        calls = []
        status = 200
        document = {}
        headers = {}

    monkeypatch.setattr(oidc.httpx, "Client", Client)
    return Client


def test_discovery_refuses_an_http_issuer_before_any_request(idp_client):
    with pytest.raises(oidc.InsecureIdPURLError):
        oidc.fetch_discovery_document("http://idp.example.com")
    assert idp_client.calls == []


@pytest.mark.parametrize("endpoint", oidc.DISCOVERY_ENDPOINTS)
def test_discovery_refuses_an_http_endpoint(monkeypatch, discovery_document, endpoint):
    _serve(monkeypatch, {**discovery_document, endpoint: "http://idp.example.com/x"})

    with pytest.raises(oidc.InsecureIdPURLError, match=endpoint):
        oidc.fetch_discovery_document(ISSUER)


def _serve_discovery(monkeypatch, document):
    _serve(monkeypatch, document)


@pytest.mark.parametrize(
    "discovered",
    [
        "https://evil.example.com",
        "https://idp.example.com/other",
        "https://IDP.example.com",
        "http://idp.example.com",
        "",
        None,
        ["https://idp.example.com"],
    ],
)
def test_discovery_refuses_another_issuer(monkeypatch, discovery_document, discovered):
    document = {**discovery_document, "issuer": discovered}
    if discovered is None:
        del document["issuer"]
    _serve_discovery(monkeypatch, document)

    with pytest.raises(oidc.IssuerMismatchError) as exc_info:
        oidc.fetch_discovery_document(ISSUER)
    assert "evil" not in str(exc_info.value)
    assert ISSUER not in str(exc_info.value)


@pytest.mark.parametrize(
    ("configured", "discovered"), [(ISSUER, ISSUER + "/"), (ISSUER + "/", ISSUER)]
)
def test_discovery_ignores_only_a_trailing_slash(
    monkeypatch, discovery_document, configured, discovered
):
    _serve_discovery(monkeypatch, {**discovery_document, "issuer": discovered})

    assert oidc.fetch_discovery_document(configured)["issuer"] == discovered


def test_the_token_and_jwks_requests_refuse_http(idp_client):
    with pytest.raises(oidc.InsecureIdPURLError):
        oidc.fetch_jwks("http://idp.example.com/jwks")
    with pytest.raises(oidc.InsecureIdPURLError):
        oidc.exchange_code_for_tokens(
            token_endpoint="http://idp.example.com/token",
            code="c",
            redirect_uri="https://app.example.com/auth/sso/callback",
            client_id=CLIENT_ID,
            client_secret="s3cret",
            code_verifier="v",
        )


# --- the address policy for requests to the IdP ---------------------------------------


def test_a_request_goes_to_the_checked_address_with_the_real_host_name(idp_client):
    idp_client.document = {"keys": []}

    oidc.fetch_jwks("https://idp.example.com/jwks")

    [call] = idp_client.calls
    # tests/conftest.py resolves every host name to this public address.
    assert call["url"] == "https://93.184.216.34:443/jwks"
    assert call["headers"] == {"Host": "idp.example.com"}
    assert call["extensions"] == {"sni_hostname": "idp.example.com"}
    assert call["client"]["follow_redirects"] is False


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/jwks",
        "https://localhost.localdomain.invalid@127.0.0.1/jwks",
        "https://10.0.0.5/jwks",
        "https://192.168.1.20:22/jwks",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/jwks",
        "https://[::ffff:127.0.0.1]/jwks",
        "https://100.64.0.1/jwks",
    ],
)
def test_requests_to_internal_addresses_are_refused(idp_client, url):
    from app.webhook_safety import UnsafeWebhookURLError

    with pytest.raises(UnsafeWebhookURLError):
        oidc.fetch_jwks(url)
    with pytest.raises(UnsafeWebhookURLError):
        oidc.fetch_discovery_document(url.rsplit("/", 1)[0])
    with pytest.raises(UnsafeWebhookURLError):
        oidc.exchange_code_for_tokens(
            token_endpoint=url,
            code="c",
            redirect_uri="https://app.example.com/auth/sso/callback",
            client_id=CLIENT_ID,
            client_secret="s3cret",
            code_verifier="v",
        )
    assert idp_client.calls == []


def test_a_host_name_resolving_to_an_internal_address_is_refused(idp_client, monkeypatch):
    import socket

    from app.webhook_safety import UnsafeWebhookURLError

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))],
    )
    with pytest.raises(UnsafeWebhookURLError):
        oidc.fetch_jwks("https://idp.example.com/jwks")
    assert idp_client.calls == []


def test_a_redirect_from_the_idp_is_not_followed(idp_client):
    idp_client.status = 302
    idp_client.headers = {"location": "https://127.0.0.1/keys"}

    with pytest.raises(oidc.httpx.HTTPStatusError):
        oidc.fetch_jwks("https://idp.example.com/jwks")
    assert len(idp_client.calls) == 1


def test_the_development_switch_opens_loopback_only(idp_client, http_localhost_allowed):
    from app.webhook_safety import UnsafeWebhookURLError

    idp_client.document = {"keys": []}
    oidc.fetch_jwks("http://localhost:8080/realms/x/certs")
    oidc.fetch_jwks("https://127.0.0.1:8443/certs")
    assert [c["url"] for c in idp_client.calls] == [
        "http://localhost:8080/realms/x/certs",
        "https://127.0.0.1:8443/certs",
    ]
    assert all(c["client"]["follow_redirects"] is False for c in idp_client.calls)

    with pytest.raises(UnsafeWebhookURLError):
        oidc.fetch_jwks("https://10.0.0.5/certs")
    with pytest.raises(UnsafeWebhookURLError):
        oidc.fetch_jwks("https://169.254.169.254/certs")


# --- the discovery cache ----------------------------------------------------------------


def test_sign_in_starts_reuse_the_discovery_document(monkeypatch, discovery_document):
    fetched = []

    def fetch(issuer):
        fetched.append(issuer)
        return discovery_document

    monkeypatch.setattr(oidc, "fetch_discovery_document", fetch)
    provider = oidc.OIDCProvider(
        oidc.OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret")
    )
    for _ in range(5):
        provider.authorization_redirect_url(
            redirect_uri="https://app.example.com/auth/sso/callback",
            state="s",
            nonce="n",
            code_verifier="v",
        )
    assert fetched == [ISSUER]

    oidc.clear_discovery_cache()
    provider.authorization_redirect_url(
        redirect_uri="https://app.example.com/auth/sso/callback",
        state="s",
        nonce="n",
        code_verifier="v",
    )
    assert fetched == [ISSUER, ISSUER]


def test_the_discovery_cache_expires_and_never_keeps_a_failure(monkeypatch, discovery_document):
    answers = [ConnectionError("down"), discovery_document, discovery_document]
    fetched = []

    def fetch(issuer):
        fetched.append(issuer)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(oidc, "fetch_discovery_document", fetch)
    with pytest.raises(ConnectionError):
        oidc.cached_discovery_document(ISSUER)
    assert oidc.cached_discovery_document(ISSUER) == discovery_document
    assert oidc.cached_discovery_document(ISSUER) == discovery_document
    assert len(fetched) == 2

    monkeypatch.setattr(oidc, "DISCOVERY_CACHE_SECONDS", 0.0)
    oidc.cached_discovery_document(ISSUER)
    assert len(fetched) == 3
