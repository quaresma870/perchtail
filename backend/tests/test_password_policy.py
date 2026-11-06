"""#245: the password policy -- its rules, and every place a password is
set (own change, first-login change, admin-created accounts, resets),
history and expiry."""

import uuid
from datetime import timedelta

import pytest
from app import password_policy, system_settings
from app.api.auth import get_current_active_user
from app.api.auth import router as auth_router
from app.api.system_settings import router as system_settings_router
from app.api.users import router as users_router
from app.auth.models import PasswordHistory, Role, User
from app.auth.providers.local import create_local_user, verify_password
from app.config import get_settings
from app.db import get_session
from app.password_policy import PasswordPolicy, PasswordPolicyError, check_password
from app.timeutils import utcnow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

DEFAULT = PasswordPolicy()
GOOD = "Lantern-Orbit-58"


def _rule(password, policy=DEFAULT, identifiers=("jdoe@example.com",)):
    try:
        check_password(password, policy, user_identifiers=identifiers)
    except PasswordPolicyError as exc:
        return exc.rule
    return None


# --- the rules -------------------------------------------------------------


@pytest.mark.parametrize(
    "password",
    [
        GOOD,
        "correct horse battery staple",
        "x7#Qm2!vLp9@",
        "Tagus river at dusk",
        "a" * 3 + "Bcdefghijk9!",  # a run inside a longer password is fine
    ],
)
def test_reasonable_passwords_pass(password):
    assert _rule(password) is None


@pytest.mark.parametrize(
    "password, rule",
    [
        ("short-pw1", "too_short"),
        ("x" * 129, "too_long"),
        ("aaaaaaaaaaaa", "repetition"),
        ("abcabcabcabc", "repetition"),
        ("Ab1Ab1Ab1Ab1", "repetition"),
        ("123456789012", "sequence"),
        ("abcdefghijkl", "sequence"),
        ("lkjihgfedcba", "sequence"),  # reversed
        ("qwertyuiop12", "sequence"),
        ("QWERTY123456", "sequence"),  # case ignored
        ("1qaz2wsx3edc", "sequence"),
        ("asdfgh098765", "sequence"),
        ("abcdef111111", "sequence"),
        ("aabbaabbaabbx", "few_distinct"),
        ("jdoe20242024!", "is_username"),
        ("JDoe!!!!20251", "is_username"),
        ("my-jdoe-password", "contains_personal"),
    ],
)
def test_each_rule_refuses_what_it_should(password, rule):
    assert _rule(password) == rule


def test_errors_never_echo_the_password():
    for password in ("aaaaaaaaaaaa", "123456789012", "my-jdoe-password", "short"):
        with pytest.raises(PasswordPolicyError) as raised:
            check_password(password, DEFAULT, user_identifiers=["jdoe@example.com"])
        assert password not in str(raised.value)


def test_the_email_domain_is_not_personal():
    # Everyone at example.com may still use "example" or "com".
    assert _rule("comet-example-trail", identifiers=["ana@example.com"]) is None


def test_display_name_words_count_as_personal():
    rule = _rule("quaresma-lantern-8", identifiers=["jq", "Joao Quaresma"])
    assert rule == "contains_personal"


def test_short_identifiers_are_ignored():
    # A two-letter username would bar too much.
    assert _rule("jq-lantern-orbit", identifiers=["jq"]) is None


def test_switching_rules_off():
    policy = PasswordPolicy(reject_simple=False, reject_personal=False)
    assert _rule("123456789012", policy) is None
    assert _rule("my-jdoe-password", policy) is None


@pytest.mark.parametrize(
    "required, password, ok",
    [
        (2, "lanternorbitfifty", False),
        (2, "lanternorbit58", True),
        (3, "lanternorbit58", False),
        (4, "Lanternorbit58", False),
        (4, GOOD, True),
    ],
)
def test_character_classes(required, password, ok):
    policy = PasswordPolicy(required_classes=required)
    assert (_rule(password, policy) is None) is ok


def test_generated_passwords_satisfy_the_strictest_policy():
    policy = PasswordPolicy(min_length=40, required_classes=4)
    for _ in range(50):
        generated = password_policy.generate_password(policy, user_identifiers=["admin"])
        assert len(generated) == 40
        check_password(generated, policy, user_identifiers=["admin"])


def test_expiry():
    user = User(username="u", role_id=1, password_hash="h", password_changed_at=utcnow())
    assert not password_policy.password_expired(user, PasswordPolicy(max_age_days=0))
    assert not password_policy.password_expired(user, PasswordPolicy(max_age_days=30))
    user.password_changed_at = utcnow() - timedelta(days=31)
    assert password_policy.password_expired(user, PasswordPolicy(max_age_days=30))
    sso = User(username="s", role_id=1, password_hash=None, password_changed_at=None)
    assert not password_policy.password_expired(sso, PasswordPolicy(max_age_days=1))


# --- the API ---------------------------------------------------------------

USERNAME = "jdoe@example.com"
TEMP = "Temporary-Pass-2026"


def _set(session, **values):
    for key, value in values.items():
        if isinstance(value, bool):
            system_settings.set_bool(session, key, value)
        else:
            system_settings.set_int(session, key, value)
    session.commit()


def _role(session, **kwargs) -> Role:
    role = Role(name=f"role-{uuid.uuid4().hex}", **kwargs)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


@pytest.fixture()
def auth_client(session, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    get_settings.cache_clear()


def _signed_in(session, auth_client, password=TEMP) -> User:
    user = create_local_user(
        session,
        actor_user_id=None,
        username=USERNAME,
        password=password,
        role_id=_role(session).id,
    )
    response = auth_client.post("/auth/login", json={"username": USERNAME, "password": password})
    assert response.status_code == 200
    return user


def _change(auth_client, current, new):
    return auth_client.post(
        "/auth/change-password", json={"current_password": current, "new_password": new}
    )


def test_the_first_login_change_enforces_the_policy(session, auth_client):
    user = _signed_in(session, auth_client)

    response = _change(auth_client, TEMP, "123456789012")
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "password_sequence"
    session.refresh(user)
    assert user.must_change_password is True
    assert verify_password(user, TEMP)

    assert _change(auth_client, TEMP, GOOD).status_code == 200
    session.refresh(user)
    assert user.must_change_password is False


def test_a_later_change_enforces_the_configured_length(session, auth_client):
    _signed_in(session, auth_client)
    assert _change(auth_client, TEMP, GOOD).status_code == 200
    _set(session, password_min_length=20)

    response = _change(auth_client, GOOD, "Lantern-Orbit-59")
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "Use at least 20 characters."
    assert _change(auth_client, GOOD, "Lantern-Orbit-Tagus-59").status_code == 200


def test_history_refuses_the_last_n_passwords(session, auth_client):
    _set(session, password_history=3)
    user = _signed_in(session, auth_client)
    passwords = [TEMP, "Lantern-Orbit-1a", "Lantern-Orbit-2b", "Lantern-Orbit-3c"]
    for current, new in zip(passwords, passwords[1:], strict=False):
        assert _change(auth_client, current, new).status_code == 200

    # The current one and the two before it are refused...
    for reused in passwords[1:]:
        response = _change(auth_client, passwords[-1], reused)
        assert response.status_code == 422
        expected = "password_same_as_current" if reused == passwords[-1] else "password_reused"
        assert response.json()["detail"]["error_code"] == expected
    # ...the one before those is allowed again.
    assert _change(auth_client, passwords[-1], TEMP).status_code == 200

    # Only hashes are kept, and only as many as the rule needs.
    history = session.exec(select(PasswordHistory).where(PasswordHistory.user_id == user.id)).all()
    assert len(history) == 2
    assert all(row.password_hash.startswith("$argon2id$") for row in history)


def test_turning_history_off_forgets_it_at_once(session, auth_client):
    _set(session, password_history=5)
    user = _signed_in(session, auth_client)
    assert _change(auth_client, TEMP, GOOD).status_code == 200
    assert _change(auth_client, GOOD, "Lantern-Orbit-77").status_code == 200

    admin = User(username="admin", role_id=_role(session, is_super_admin=True).id)
    session.add(admin)
    session.commit()
    app = FastAPI()
    app.include_router(system_settings_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: admin
    settings_client = TestClient(app)

    def history():
        query = select(PasswordHistory).where(PasswordHistory.user_id == user.id)
        return session.exec(query).all()

    assert len(history()) == 2
    assert (
        settings_client.patch("/system-settings", json={"password_history": 2}).status_code == 200
    )
    assert len(history()) == 1
    assert (
        settings_client.patch("/system-settings", json={"password_history": 0}).status_code == 200
    )
    assert history() == []


def test_a_forced_change_cant_keep_the_same_password(session, auth_client):
    # With history off, still: the point of a forced change (first login,
    # reset, expiry) is a different password.
    _signed_in(session, auth_client)
    response = _change(auth_client, TEMP, TEMP)
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "password_same_as_current"


def test_a_refused_change_keeps_the_other_sessions(session, auth_client):
    user = _signed_in(session, auth_client)
    other = TestClient(auth_client.app)
    assert (
        other.post("/auth/login", json={"username": USERNAME, "password": TEMP}).status_code == 200
    )

    assert _change(auth_client, TEMP, "123456789012").status_code == 422
    assert other.get("/auth/me").status_code == 200

    assert _change(auth_client, TEMP, GOOD).status_code == 200
    assert other.get("/auth/me").status_code == 401
    session.refresh(user)
    assert verify_password(user, GOOD)


def test_an_expired_password_must_be_changed_at_login(session, auth_client):
    user = _signed_in(session, auth_client)
    assert _change(auth_client, TEMP, GOOD).status_code == 200
    _set(session, password_max_age_days=90)
    user.password_changed_at = utcnow() - timedelta(days=91)
    session.add(user)
    session.commit()

    response = auth_client.post("/auth/login", json={"username": USERNAME, "password": GOOD})
    assert response.status_code == 200
    assert response.json()["must_change_password"] is True

    assert _change(auth_client, GOOD, "Lantern-Orbit-77").status_code == 200
    session.refresh(user)
    assert user.must_change_password is False
    assert utcnow() - user.password_changed_at < timedelta(minutes=1)


def test_the_policy_is_readable_before_the_first_change(session, auth_client):
    _signed_in(session, auth_client)
    response = auth_client.get("/auth/password-policy")
    assert response.status_code == 200
    assert response.json() == DEFAULT.as_dict()


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


def test_an_admin_created_password_must_follow_the_policy(session, admin_client):
    role = _role(session)
    response = admin_client.post(
        "/users",
        json={"username": "ana@example.com", "password": "ana-2026-pass", "role_id": role.id},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "password_contains_personal"
    assert session.exec(select(User).where(User.username == "ana@example.com")).first() is None

    response = admin_client.post(
        "/users", json={"username": "ana@example.com", "password": TEMP, "role_id": role.id}
    )
    assert response.status_code == 201


def test_a_reset_password_follows_the_policy(session, admin_client):
    _set(session, password_min_length=24, password_required_classes=4)
    target = create_local_user(
        session,
        actor_user_id=None,
        username="bea@example.com",
        password=TEMP,
        role_id=_role(session).id,
    )
    response = admin_client.post(f"/users/{target.id}/reset-password")
    assert response.status_code == 200
    temporary = response.json()["temporary_password"]
    check_password(
        temporary, password_policy.get_policy(session), user_identifiers=["bea@example.com"]
    )
    session.refresh(target)
    assert verify_password(target, temporary) and target.must_change_password
