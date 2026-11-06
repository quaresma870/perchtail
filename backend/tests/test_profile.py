"""#243: My profile -- display name and email, for the user (PATCH
/auth/me), for admins (Users API) and from SSO claims."""

import uuid

import pytest
from app import password_policy, profile
from app.api.auth import get_current_active_user
from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.auth.models import AuditLog, AuthProviderType, Role, User
from app.auth.providers.local import create_local_user
from app.config import get_settings
from app.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

PASSWORD = "Temporary-Pass-2026"


def _role(session, **kwargs) -> Role:
    role = Role(name=f"role-{uuid.uuid4().hex}", **kwargs)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def _events(session, action):
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Ana   Silva ", "Ana Silva"),
        ("", None),
        ("   ", None),
        (None, None),
        ("João Quaresma", "João Quaresma"),
    ],
)
def test_display_name_is_tidied(raw, expected):
    assert profile.normalize_display_name(raw) == expected


@pytest.mark.parametrize("raw", ["x" * 101, "Ana\nSilva", "Ana\x00"])
def test_display_name_refuses(raw):
    with pytest.raises(profile.ProfileError):
        profile.normalize_display_name(raw)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" Ana.Silva@Example.COM ", "ana.silva@example.com"),
        ("", None),
        (None, None),
        ("a+tag@sub.example.pt", "a+tag@sub.example.pt"),
    ],
)
def test_email_is_normalized(raw, expected):
    assert profile.normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ana",
        "ana@",
        "@example.com",
        "ana@example",
        "ana silva@example.com",
        "a@@b.com",
        "ana@example.",
        "x" * 65 + "@example.com",
    ],
)
def test_email_refuses_what_isnt_an_address(raw):
    with pytest.raises(profile.ProfileError):
        profile.normalize_email(raw)


# --- PATCH /auth/me ----------------------------------------------------------


@pytest.fixture()
def me_client(session):
    """A client signed in as `holder["user"]`."""
    holder: dict = {}
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: holder["user"]
    client = TestClient(app)
    client.holder = holder
    return client


def _local(session, username="jdoe", **kwargs) -> User:
    user = create_local_user(
        session,
        actor_user_id=None,
        username=username,
        password=PASSWORD,
        role_id=_role(session).id,
        **kwargs,
    )
    user.must_change_password = False
    session.add(user)
    session.commit()
    return user


def test_a_user_sets_their_name_and_email(session, me_client):
    user = me_client.holder["user"] = _local(session)

    response = me_client.patch(
        "/auth/me", json={"display_name": " Jo Doe ", "email": "JDoe@Example.com"}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["display_name"], body["email"]) == ("Jo Doe", "jdoe@example.com")
    assert body["auth_provider"] == "local"

    (name_event,) = _events(session, "user.profile_update")
    assert name_event.user_id == user.id and name_event.target_id == user.id
    (email_event,) = _events(session, "user.email_change")
    assert email_event.event_metadata["old_email"] is None
    assert email_event.event_metadata["new_email"] == "jdoe@example.com"


def test_an_omitted_field_is_kept_and_an_empty_one_cleared(session, me_client):
    me_client.holder["user"] = _local(session, display_name="Jo", email="jo@example.com")

    body = me_client.patch("/auth/me", json={"display_name": "Joana"}).json()
    assert (body["display_name"], body["email"]) == ("Joana", "jo@example.com")

    body = me_client.patch("/auth/me", json={"email": ""}).json()
    assert body["email"] is None
    assert len(_events(session, "user.email_change")) == 1


def test_an_unchanged_value_isnt_audited(session, me_client):
    me_client.holder["user"] = _local(session, email="jo@example.com")
    assert me_client.patch("/auth/me", json={"email": "JO@example.com"}).status_code == 200
    assert _events(session, "user.email_change") == []


def test_an_email_is_unique_whatever_its_case(session, me_client):
    _local(session, username="other", email="taken@example.com")
    me_client.holder["user"] = _local(session)

    response = me_client.patch("/auth/me", json={"email": "Taken@Example.com"})
    assert response.status_code == 409
    response = me_client.patch("/auth/me", json={"email": "not-an-email"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Enter a valid email address."


def test_an_sso_account_cant_edit_its_profile(session, me_client):
    sso = User(
        username="sso@example.com",
        role_id=_role(session).id,
        auth_provider=AuthProviderType.oidc,
        external_issuer="https://idp.example",
        external_id="abc",
    )
    session.add(sso)
    session.commit()
    me_client.holder["user"] = sso

    assert me_client.patch("/auth/me", json={"display_name": "X"}).status_code == 409


def test_me_returns_the_profile(session, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    _local(session, display_name="Jo Doe", email="jo@example.com")
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    assert (
        client.post("/auth/login", json={"username": "jdoe", "password": PASSWORD}).status_code
        == 200
    )
    body = client.get("/auth/me").json()
    assert (body["display_name"], body["email"], body["auth_provider"]) == (
        "Jo Doe",
        "jo@example.com",
        "local",
    )
    get_settings.cache_clear()


def test_the_password_policy_knows_the_name_and_email(session):
    user = _local(session, display_name="Ana Quaresma", email="anaq@example.com")
    identifiers = password_policy.user_identifiers(user)
    policy = password_policy.PasswordPolicy()
    for password in ("quaresma-lantern-8", "anaq-lantern-orbit"):
        with pytest.raises(password_policy.PasswordPolicyError):
            password_policy.check_password(password, policy, user_identifiers=identifiers)


# --- the Users API -----------------------------------------------------------


@pytest.fixture()
def admin_client(session):
    admin = User(username="admin", role_id=_role(session, is_super_admin=True).id)
    session.add(admin)
    session.commit()
    app = FastAPI()
    app.include_router(users_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: admin
    return TestClient(app)


def test_an_admin_creates_a_user_with_name_and_email(session, admin_client):
    role = _role(session)
    response = admin_client.post(
        "/users",
        json={
            "username": "ana",
            "password": PASSWORD,
            "role_id": role.id,
            "display_name": "Ana Silva",
            "email": "Ana@Example.com",
        },
    )
    assert response.status_code == 201
    assert (response.json()["display_name"], response.json()["email"]) == (
        "Ana Silva",
        "ana@example.com",
    )
    listed = {u["username"]: u for u in admin_client.get("/users").json()}
    assert listed["ana"]["email"] == "ana@example.com"

    duplicate = admin_client.post(
        "/users",
        json={
            "username": "ana2",
            "password": PASSWORD,
            "role_id": role.id,
            "email": "ana@example.com",
        },
    )
    assert duplicate.status_code == 409
    bad = admin_client.post(
        "/users",
        json={"username": "ana3", "password": PASSWORD, "role_id": role.id, "email": "nope"},
    )
    assert bad.status_code == 422
    assert session.exec(select(User).where(User.username == "ana3")).first() is None


def test_an_admin_edits_a_local_users_profile_but_not_an_sso_one(session, admin_client):
    target = _local(session, username="bea")
    response = admin_client.patch(f"/users/{target.id}", json={"email": "bea@example.com"})
    assert response.status_code == 200
    assert response.json()["email"] == "bea@example.com"
    (event,) = _events(session, "user.email_change")
    assert event.target_id == target.id and event.user_id != target.id

    sso = User(
        username="sso@example.com",
        role_id=_role(session).id,
        auth_provider=AuthProviderType.oidc,
        external_issuer="https://idp.example",
        external_id="abc",
    )
    session.add(sso)
    session.commit()
    assert admin_client.patch(f"/users/{sso.id}", json={"display_name": "X"}).status_code == 409


# --- SSO claims --------------------------------------------------------------


def _sso(session) -> User:
    user = User(
        username="sub-123",
        role_id=_role(session).id,
        auth_provider=AuthProviderType.oidc,
        external_issuer="https://idp.example",
        external_id="sub-123",
    )
    session.add(user)
    session.commit()
    return user


def test_sso_claims_set_the_name_and_email(session):
    user = _sso(session)
    profile.sync_from_claims(
        session, user, {"name": "Ana Silva", "email": "Ana@Example.com", "email_verified": True}
    )
    assert (user.display_name, user.email) == ("Ana Silva", "ana@example.com")


def test_sso_name_falls_back_to_given_and_family_names(session):
    user = _sso(session)
    profile.sync_from_claims(session, user, {"given_name": "Ana", "family_name": "Silva"})
    assert user.display_name == "Ana Silva"


def test_sso_skips_an_unverified_taken_or_malformed_email(session):
    _local(session, username="other", email="taken@example.com")
    user = _sso(session)
    for claims in (
        {"email": "new@example.com", "email_verified": False},
        {"email": "TAKEN@example.com"},
        {"email": "not an email"},
    ):
        profile.sync_from_claims(session, user, claims)
        assert user.email is None


def test_sso_treats_a_string_false_as_unverified(session):
    user = _sso(session)
    profile.sync_from_claims(session, user, {"email": "ana@example.com", "email_verified": "false"})
    assert user.email is None
    profile.sync_from_claims(session, user, {"email": "ana@example.com", "email_verified": "true"})
    assert user.email == "ana@example.com"


def test_short_name_words_dont_bar_passwords():
    identifiers = ["bob", "Bob The Admin"]
    policy = password_policy.PasswordPolicy()
    # "the" (3 letters) is allowed; "admin" (a 5-letter word of the name) isn't.
    password_policy.check_password("lantern-the-orbit", policy, user_identifiers=identifiers)
    with pytest.raises(password_policy.PasswordPolicyError):
        password_policy.check_password("lantern-admin-orbit", policy, user_identifiers=identifiers)


def test_a_profile_only_edit_isnt_also_a_user_update(session, admin_client):
    target = _local(session, username="cid")
    assert (
        admin_client.patch(f"/users/{target.id}", json={"display_name": "Cid"}).status_code == 200
    )
    assert _events(session, "user.update") == []
    assert len(_events(session, "user.profile_update")) == 1
