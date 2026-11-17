"""First sign-in, address-aware throttling, stricter SSO accounts, sessions
ended on deactivation, and mandatory MFA (T-0017)."""

import os
import stat
import time
import uuid
from unittest.mock import patch

import pyotp
import pytest
from app import initial_admin_password, system_settings
from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.auth import mfa_policy
from app.auth.models import (
    AuditLog,
    AuthProviderType,
    AuthSession,
    GlobalCapability,
    Role,
    User,
)
from app.auth.providers.local import create_local_user, hash_password
from app.bootstrap import seed_initial_super_admin
from app.config import get_settings
from app.db import get_session
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

PASSWORD = "Correct-Horse-2026!"
NEW_PASSWORD = "Tagus-River-new-73!"
IP_A = ("203.0.113.10", 50000)
IP_B = ("198.51.100.20", 50000)


@pytest.fixture()
def make_client(session, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(users_router)

    from app.api.auth import get_current_active_user

    @app.get("/protected")
    def protected(user=Depends(get_current_active_user)):
        return {"username": user.username}

    app.dependency_overrides[get_session] = lambda: session

    def _make(client=("testclient", 50000)):
        return TestClient(app, client=client)

    yield _make
    get_settings.cache_clear()


def _role(session, *, super_admin=False, capabilities=None) -> Role:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=super_admin,
        global_capabilities=capabilities or [],
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def _local_user(session, username, *, role=None, must_change=False) -> User:
    user = create_local_user(
        session,
        actor_user_id=None,
        username=username,
        password=PASSWORD,
        role_id=(role or _role(session)).id,
    )
    user.must_change_password = must_change
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _sso_user(session, username, *, password_hash=None) -> User:
    user = User(
        username=username,
        role_id=_role(session).id,
        auth_provider=AuthProviderType.oidc,
        external_issuer="https://idp.example.com",
        external_id=f"sub-{username}",
        password_hash=password_hash,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _login(client, username, password=PASSWORD, mfa_code=None):
    body = {"username": username, "password": password}
    if mfa_code is not None:
        body["mfa_code"] = mfa_code
    return client.post("/auth/login", json=body)


def _audit(session, action):
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


def _details(entry) -> dict:
    """The entry's metadata without the target name the audit log adds."""
    return {k: v for k, v in entry.event_metadata.items() if k != "target_name"}


def initial_admin_password_path():
    # Through the module: tests/conftest.py points it at tmp_path.
    return initial_admin_password.initial_admin_password_path()


# --- the first super admin's password -----------------------------------------------


def test_the_initial_password_goes_to_a_private_file_not_the_log(session, caplog, capsys):
    with patch("app.bootstrap.password_policy.generate_password", return_value="Fixed-Temp-9x!"):
        user = seed_initial_super_admin(session)

    path = initial_admin_password_path()
    assert path.read_text(encoding="utf-8") == "Fixed-Temp-9x!\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert user.must_change_password is True
    logged = caplog.text + "".join(capsys.readouterr())
    assert "Fixed-Temp-9x!" not in logged
    assert str(path) in logged


def test_the_file_is_deleted_at_the_first_password_change(session, make_client):
    with patch("app.bootstrap.password_policy.generate_password", return_value="Fixed-Temp-9x!"):
        seed_initial_super_admin(session)
    path = initial_admin_password_path()
    client = make_client()
    assert _login(client, "admin", "Fixed-Temp-9x!").status_code == 200
    assert path.exists()

    response = client.post(
        "/auth/change-password",
        json={"current_password": "Fixed-Temp-9x!", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 200
    assert not path.exists()


def test_another_accounts_password_change_leaves_the_file(session, make_client):
    seed_initial_super_admin(session)
    _local_user(session, "someone")
    client = make_client()
    _login(client, "someone")

    response = client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 200
    assert initial_admin_password_path().exists()


def test_a_reset_of_the_first_admin_deletes_the_file(session, make_client):
    first = seed_initial_super_admin(session)
    _local_user(session, "second-admin", role=_role(session, super_admin=True))
    client = make_client()
    _login(client, "second-admin")

    response = client.post(f"/users/{first.id}/reset-password")

    assert response.status_code == 200
    assert not initial_admin_password_path().exists()


# --- throttling by address --------------------------------------------------------


@pytest.fixture()
def low_limits(monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()


def test_an_address_that_runs_out_of_attempts_does_not_block_others(
    session, make_client, low_limits
):
    _local_user(session, "alice")
    attacker = make_client(IP_A)
    # Sprays made-up usernames: LOGIN_MAX_ATTEMPTS failures lock the
    # address, whatever the usernames.
    for i in range(3):
        assert _login(attacker, f"made-up-{i}", "nope").status_code == 401
    assert _login(attacker, "alice").status_code == 429

    assert _login(make_client(IP_B), "alice").status_code == 200
    locks = [e.event_metadata["scope"] for e in _audit(session, "user.locked_out")]
    assert "address" in locks


def test_ten_thousand_made_up_usernames_from_one_address_block_no_one_else(
    session, make_client, monkeypatch
):
    # The account table is bounded; full, it used to refuse every new username.
    monkeypatch.setattr("app.login_throttle._MAX_TRACKED_ACCOUNTS", 20)
    _local_user(session, "alice")
    attacker = make_client(IP_A)
    for i in range(40):
        _login(attacker, f"made-up-{i}", "nope")

    assert _login(make_client(IP_B), "alice").status_code == 200


def test_the_emergency_account_still_signs_in_from_another_address(
    session, make_client, low_limits
):
    _local_user(session, "admin", role=_role(session, super_admin=True))
    owner = make_client()
    _login(owner, "admin")
    _, backup_codes = _enroll(owner)
    attacker = make_client(IP_A)
    for _ in range(3):
        assert _login(attacker, "admin", "nope").status_code == 401
    assert _login(attacker, "admin").status_code == 429

    response = _login(make_client(IP_B), "admin", mfa_code=backup_codes[0])
    assert response.status_code == 200
    # The same failures also reach the address limit (both are
    # LOGIN_MAX_ATTEMPTS), which locks only that address too.
    scopes = sorted(e.event_metadata["scope"] for e in _audit(session, "user.locked_out"))
    assert scopes == ["account_and_address", "address"]


def test_a_super_admin_without_mfa_on_is_locked_from_every_address(
    session, make_client, low_limits
):
    """Without MFA, nothing but the per-username limit stands against
    guessing its password from many addresses."""
    admin = _local_user(session, "admin", role=_role(session, super_admin=True))
    assert not mfa_policy.emergency_account(admin)
    for i in range(3):
        response = _login(make_client((f"203.0.113.{i + 1}", 50000)), "admin", "nope")
        assert response.status_code == 401

    assert _login(make_client(IP_B), "admin").status_code == 429
    [lock] = _audit(session, "user.locked_out")
    assert lock.event_metadata["scope"] == "account"


def test_an_ordinary_account_is_locked_from_every_address(session, make_client, low_limits):
    _local_user(session, "alice")
    for i in range(3):
        _login(make_client((f"203.0.113.{i + 1}", 50000)), "alice", "nope")

    assert _login(make_client(IP_B), "alice").status_code == 429


# --- SSO accounts have no local password ------------------------------------------


def test_an_sso_account_with_a_password_cannot_sign_in_locally(session, make_client):
    _sso_user(session, "sso@example.com", password_hash=hash_password(PASSWORD))
    client = make_client()

    response = _login(client, "sso@example.com")
    wrong = _login(client, "nobody@example.com")

    # The same answer as a wrong password, and audited the same way.
    assert response.status_code == wrong.status_code == 401
    assert response.json() == wrong.json()
    assert "perchtail_session" not in response.cookies
    failures = _audit(session, "user.login_failed")
    assert failures[0].event_metadata["reason"] == "bad_credentials"


def test_an_sso_account_costs_the_same_argon2_work(session, make_client, monkeypatch):
    from app.auth.providers import local

    calls = []
    monkeypatch.setattr(local, "_burn_a_verify", lambda password: calls.append(password))
    _sso_user(session, "sso@example.com", password_hash=hash_password(PASSWORD))

    _login(make_client(), "sso@example.com")

    assert calls == [PASSWORD]


def test_resetting_an_sso_accounts_password_is_refused_and_audited(session, make_client):
    target = _sso_user(session, "sso@example.com")
    admin = _local_user(session, "root", role=_role(session, super_admin=True))
    client = make_client()
    _login(client, "root")

    response = client.post(f"/users/{target.id}/reset-password")

    assert response.status_code == 409
    session.refresh(target)
    assert target.password_hash is None
    [denied] = _audit(session, "access.denied")
    assert denied.user_id == admin.id
    assert denied.target_id == target.id
    assert denied.event_metadata["reason"] == "sso_account"
    assert _audit(session, "user.reset_password") == []


# --- deactivation ends sessions ---------------------------------------------------


@pytest.mark.parametrize("how", ["delete", "patch"])
def test_deactivating_a_user_ends_their_sessions_at_once(session, make_client, how):
    _local_user(session, "root", role=_role(session, super_admin=True))
    target = _local_user(session, "alice")
    admin_client = make_client()
    _login(admin_client, "root")
    user_client = make_client(IP_B)
    _login(user_client, "alice")
    assert user_client.get("/protected").status_code == 200

    if how == "delete":
        assert admin_client.delete(f"/users/{target.id}").status_code == 204
    else:
        response = admin_client.patch(f"/users/{target.id}", json={"active": False})
        assert response.status_code == 200

    assert user_client.get("/protected").status_code == 401
    assert session.exec(select(AuthSession).where(AuthSession.user_id == target.id)).all() == []
    [revoke] = [e for e in _audit(session, "session.revoke") if e.target_id == target.id]
    assert _details(revoke) == {"count": 1, "reason": "deactivated"}

    # Reactivating the account doesn't bring the old session back.
    response = admin_client.patch(f"/users/{target.id}", json={"active": True})
    assert response.status_code == 200
    assert user_client.get("/protected").status_code == 401


# --- MFA backup codes without the TOTP secret -------------------------------------


def _enroll(client, password=PASSWORD) -> tuple[str, list[str]]:
    secret = client.post("/auth/mfa/enroll", json={"password": password}).json()["secret"]
    confirm = client.post("/auth/mfa/confirm", json={"code": pyotp.TOTP(secret).now()})
    return secret, confirm.json()["backup_codes"]


def _break_the_secret(session, username):
    user = session.exec(select(User).where(User.username == username)).one()
    user.mfa_secret_encrypted = "gAAAAA-not-a-valid-fernet-token"
    session.add(user)
    session.commit()


def test_a_backup_code_signs_in_when_the_secret_cannot_be_decrypted(session, make_client):
    _local_user(session, "alice")
    client = make_client()
    _login(client, "alice")
    _, backup_codes = _enroll(client)
    _break_the_secret(session, "alice")

    response = _login(make_client(IP_B), "alice", mfa_code=backup_codes[0])

    assert response.status_code == 200
    assert _audit(session, "user.mfa_secret_unreadable") == []


def test_a_totp_code_with_an_unreadable_secret_is_a_neutral_refusal(session, make_client):
    alice = _local_user(session, "alice")
    client = make_client()
    _login(client, "alice")
    secret, _ = _enroll(client)
    _break_the_secret(session, "alice")

    code = pyotp.TOTP(secret).at(time.time(), counter_offset=1)
    response = _login(make_client(IP_B), "alice", mfa_code=code)

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "mfa_invalid_code"
    [entry] = _audit(session, "user.mfa_secret_unreadable")
    assert entry.target_id == alice.id


# --- mandatory MFA ----------------------------------------------------------------


@pytest.fixture()
def enforced(monkeypatch):
    monkeypatch.setattr(mfa_policy, "ENFORCED", True)


def test_an_admin_account_without_mfa_can_only_set_it_up(session, make_client, enforced):
    admin = _local_user(session, "root", role=_role(session, super_admin=True))
    client = make_client()

    response = _login(client, "root")

    assert response.status_code == 200
    assert response.json()["mfa_required"] is True
    assert response.json()["mfa_enabled"] is False
    [required] = _audit(session, "user.mfa_required")
    assert required.target_id == admin.id
    assert _details(required) == {"reason": "admin_account"}
    refused = client.get("/protected")
    assert refused.status_code == 403
    assert refused.json()["detail"]["error_code"] == "mfa_setup_required"
    assert client.get("/auth/me").status_code == 200
    # The refusal is a step to finish, not a denied permission.
    assert _audit(session, "access.denied") == []

    _enroll(client)

    assert client.get("/protected").status_code == 200
    [enabled] = _audit(session, "user.mfa_enable")
    assert _details(enabled) == {"required": True}


@pytest.mark.parametrize(
    "capabilities",
    [[GlobalCapability.manage_users], [GlobalCapability.view_audit_log]],
)
def test_any_global_capability_makes_mfa_mandatory(session, make_client, enforced, capabilities):
    _local_user(session, "delegated", role=_role(session, capabilities=capabilities))
    client = make_client()
    _login(client, "delegated")

    assert client.get("/protected").status_code == 403


def test_other_local_accounts_need_mfa_only_with_the_setting(session, make_client, enforced):
    _local_user(session, "alice")
    client = make_client()
    _login(client, "alice")
    assert client.get("/protected").status_code == 200

    system_settings.set_bool(session, "require_mfa_local_accounts", True)
    session.commit()

    # Applied at once, on the next request.
    assert client.get("/protected").status_code == 403
    assert client.get("/auth/me").json()["mfa_required"] is True


def test_sso_accounts_are_never_asked_for_mfa(session, enforced):
    system_settings.set_bool(session, "require_mfa_local_accounts", True)
    session.commit()
    user = _sso_user(session, "sso@example.com")
    user.role.is_super_admin = True
    assert mfa_policy.mfa_required(session, user) is False


def test_the_forced_password_change_comes_before_mfa_setup(session, make_client, enforced):
    _local_user(session, "root", role=_role(session, super_admin=True), must_change=True)
    client = make_client()
    _login(client, "root")

    refused = client.post("/auth/mfa/enroll", json={"password": PASSWORD})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Password change required"

    client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert client.get("/protected").json()["detail"]["error_code"] == "mfa_setup_required"
    _enroll(client, NEW_PASSWORD)
    assert client.get("/protected").status_code == 200


# --- the longest password a request may carry -----------------------------------


@pytest.fixture()
def no_password_checks(monkeypatch):
    """Fails the test if a too-long password reaches argon2."""

    def refuse(*args, **kwargs):
        pytest.fail("a too-long password reached the password check")

    monkeypatch.setattr("app.api.auth.verify_password", refuse)
    monkeypatch.setattr("app.api.auth.LocalPasswordProvider.authenticate", refuse)
    monkeypatch.setattr("app.api.auth.change_password", refuse)


def _too_long(response) -> bool:
    return (
        response.status_code == 422
        and response.json()["detail"]["error_code"] == "password_too_long"
    )


def test_a_huge_password_on_login_is_refused_before_argon2(
    session, make_client, no_password_checks
):
    _local_user(session, "ana")
    response = _login(make_client(), "ana", "x" * 1_000_000)
    assert _too_long(response)
    assert "x" * 200 not in response.text


def test_huge_passwords_after_sign_in_are_refused_before_argon2(session, make_client, monkeypatch):
    _local_user(session, "ana")
    client = make_client()
    assert _login(client, "ana").status_code == 200

    def refuse(*args, **kwargs):
        pytest.fail("a too-long password reached the password check")

    monkeypatch.setattr("app.api.auth.verify_password", refuse)
    monkeypatch.setattr("app.api.auth.change_password", refuse)
    huge = "x" * 1_000_000
    for path, body in [
        ("/auth/change-password", {"current_password": huge, "new_password": NEW_PASSWORD}),
        ("/auth/change-password", {"current_password": PASSWORD, "new_password": huge}),
        ("/auth/mfa/enroll", {"password": huge}),
        ("/auth/mfa/disable", {"password": huge}),
        ("/auth/mfa/backup-codes/regenerate", {"password": huge}),
    ]:
        assert _too_long(client.post(path, json=body)), path
    response = client.patch("/auth/me", json={"email": "ana@example.com", "current_password": huge})
    assert _too_long(response)


def test_the_limit_is_the_policy_maximum(session, make_client, no_password_checks):
    system_settings.set_int(session, "password_max_length", 64)
    session.commit()
    _local_user(session, "ana")

    response = _login(make_client(), "ana", "x" * 65)

    assert _too_long(response)
    assert response.json()["detail"]["message"] == "Use at most 64 characters."


def test_a_password_at_the_limit_is_checked_as_usual(session, make_client):
    _local_user(session, "ana")
    assert _login(make_client(), "ana", "x" * 128).status_code == 401


def test_without_a_policy_maximum_the_limit_is_1024(session, monkeypatch):
    from app import password_policy
    from app.api.auth import password_length_limit

    monkeypatch.setattr(
        password_policy, "get_policy", lambda s: password_policy.PasswordPolicy(max_length=0)
    )
    assert password_length_limit(session) == 1024


# --- a password change gives the session a new token -----------------------------


def test_changing_the_password_replaces_the_current_session_token(session, make_client):
    from app.api.auth import SESSION_COOKIE_NAME

    _local_user(session, "ana")
    client = make_client()
    assert _login(client, "ana").status_code == 200
    old_token = client.cookies[SESSION_COOKIE_NAME]
    other = make_client()
    assert _login(other, "ana").status_code == 200

    response = client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 200
    new_token = client.cookies[SESSION_COOKIE_NAME]
    assert new_token != old_token
    # This browser goes on with the new token; the old one, and the other
    # session, stop working.
    assert client.get("/auth/me").status_code == 200
    stale = make_client()
    stale.cookies.set(SESSION_COOKIE_NAME, old_token)
    assert stale.get("/auth/me").status_code == 401
    assert other.get("/auth/me").status_code == 401
    session.expire_all()
    [revoked] = _audit(session, "session.revoke")
    assert _details(revoked) == {"count": 1, "reason": "password_change"}
    user = session.exec(select(User).where(User.username == "ana")).one()
    assert len(session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).all()) == 1
