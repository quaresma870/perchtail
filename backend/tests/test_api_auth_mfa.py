import time
import uuid

import pyotp
import pytest
from app.api.auth import router as auth_router
from app.auth.models import AuditLog, Role, User
from app.auth.providers.local import create_local_user
from app.config import get_settings
from app.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

USERNAME = "jdoe@example.com"
PASSWORD = "s3cret!"


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(auth_router)

    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)

    get_settings.cache_clear()


def _make_user(session, *, username: str = USERNAME, password: str = PASSWORD) -> User:
    role = Role(name=f"Support-{uuid.uuid4().hex}")
    session.add(role)
    session.commit()
    session.refresh(role)
    return create_local_user(
        session, actor_user_id=None, username=username, password=password, role_id=role.id
    )


def _login(
    client, *, username: str = USERNAME, password: str = PASSWORD, mfa_code: str | None = None
):
    body = {"username": username, "password": password}
    if mfa_code is not None:
        body["mfa_code"] = mfa_code
    return client.post("/auth/login", json=body)


def _login_and_change_password(client, session, *, username: str = USERNAME):
    """Most MFA endpoints sit behind get_current_active_user, which blocks
    until must_change_password clears -- every admin-created test user
    starts with it set, so this is the shared setup step."""
    _make_user(session, username=username)
    _login(client, username=username)
    client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "new-password!"},
    )


def _enroll_and_confirm(client, *, password: str = "new-password!") -> tuple[str, list[str]]:
    """Runs the enroll -> confirm flow and returns (secret, backup_codes)."""
    enroll = client.post("/auth/mfa/enroll", json={"password": password})
    secret = enroll.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirm = client.post("/auth/mfa/confirm", json={"code": code})
    return secret, confirm.json()["backup_codes"]


def _next_totp_code(secret: str) -> str:
    """A code for the time-step after "now" -- confirming enrollment already
    consumes "now"'s step (see app/auth/mfa.py's replay protection), so a
    login attempt made moments later in the same test needs a fresh one."""
    return pyotp.TOTP(secret).at(time.time(), counter_offset=1)


# --- enroll ---------------------------------------------------------------


def test_mfa_enroll_requires_authentication(client):
    response = client.post("/auth/mfa/enroll", json={"password": PASSWORD})
    assert response.status_code == 401


def test_mfa_enroll_returns_a_secret_and_otpauth_uri(client, session):
    _login_and_change_password(client, session)

    response = client.post("/auth/mfa/enroll", json={"password": "new-password!"})

    assert response.status_code == 200
    body = response.json()
    assert body["secret"]
    assert body["otpauth_uri"].startswith("otpauth://totp/")


def test_mfa_enroll_does_not_enable_mfa_yet(client, session):
    _login_and_change_password(client, session)
    client.post("/auth/mfa/enroll", json={"password": "new-password!"})

    me = client.get("/auth/me")
    assert me.json()["mfa_enabled"] is False


def test_mfa_enroll_requires_correct_password(client, session):
    """A stolen session cookie alone shouldn't be enough to enroll a new
    authenticator device -- see MfaPasswordConfirmRequest's docstring."""
    _login_and_change_password(client, session)

    response = client.post("/auth/mfa/enroll", json={"password": "wrong"})

    assert response.status_code == 401
    me = client.get("/auth/me")
    assert me.json()["mfa_enabled"] is False


def test_mfa_enroll_rejects_re_enrollment_while_already_enabled(client, session):
    """Re-enrolling over an active secret would silently replace it before a
    replacement is confirmed to work, bricking the current second factor --
    disabling first is required instead."""
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)

    response = client.post("/auth/mfa/enroll", json={"password": "new-password!"})

    assert response.status_code == 400


# --- confirm ----------------------------------------------------------------


def test_mfa_confirm_with_valid_code_enables_mfa_and_returns_backup_codes(client, session):
    _login_and_change_password(client, session)
    enroll = client.post("/auth/mfa/enroll", json={"password": "new-password!"})
    secret = enroll.json()["secret"]

    response = client.post("/auth/mfa/confirm", json={"code": pyotp.TOTP(secret).now()})

    assert response.status_code == 200
    codes = response.json()["backup_codes"]
    assert len(codes) == 10

    me = client.get("/auth/me")
    assert me.json()["mfa_enabled"] is True


def test_mfa_confirm_with_invalid_code_returns_400_and_does_not_enable_mfa(client, session):
    _login_and_change_password(client, session)
    client.post("/auth/mfa/enroll", json={"password": "new-password!"})

    response = client.post("/auth/mfa/confirm", json={"code": "000000"})

    assert response.status_code == 400
    me = client.get("/auth/me")
    assert me.json()["mfa_enabled"] is False


def test_mfa_confirm_audits_enable(client, session):
    _login_and_change_password(client, session)
    enroll = client.post("/auth/mfa/enroll", json={"password": "new-password!"})
    secret = enroll.json()["secret"]

    client.post("/auth/mfa/confirm", json={"code": pyotp.TOTP(secret).now()})

    actions = [e.action for e in session.exec(select(AuditLog)).all()]
    assert "user.mfa_enable" in actions


# --- login with MFA ---------------------------------------------------------


def test_login_with_mfa_enabled_requires_a_code(client, session):
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)
    client.post("/auth/logout")

    response = _login(client, password="new-password!")

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "mfa_required"
    assert "perchtail_session" not in response.cookies


def test_login_with_mfa_enabled_and_a_valid_code_succeeds(client, session):
    _login_and_change_password(client, session)
    secret, _ = _enroll_and_confirm(client)
    client.post("/auth/logout")

    response = _login(client, password="new-password!", mfa_code=_next_totp_code(secret))

    assert response.status_code == 200
    assert "perchtail_session" in response.cookies


def test_login_with_mfa_enabled_and_an_invalid_code_fails(client, session):
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)
    client.post("/auth/logout")

    response = _login(client, password="new-password!", mfa_code="000000")

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "mfa_invalid_code"
    assert "perchtail_session" not in response.cookies


def test_login_with_mfa_enabled_accepts_a_backup_code(client, session):
    _login_and_change_password(client, session)
    _, codes = _enroll_and_confirm(client)
    client.post("/auth/logout")

    response = _login(client, password="new-password!", mfa_code=codes[0])

    assert response.status_code == 200
    assert "perchtail_session" in response.cookies


def test_login_with_mfa_enabled_rejects_a_reused_backup_code(client, session):
    _login_and_change_password(client, session)
    _, codes = _enroll_and_confirm(client)
    client.post("/auth/logout")

    first = _login(client, password="new-password!", mfa_code=codes[0])
    assert first.status_code == 200
    client.post("/auth/logout")

    second = _login(client, password="new-password!", mfa_code=codes[0])
    assert second.status_code == 401


def test_login_with_mfa_enabled_rejects_a_reused_totp_code(client, session):
    """pyotp's own verify() accepts a code for its whole ~90s validity
    window regardless of prior use -- User.mfa_last_used_step is what
    actually makes a live code single-use, same as MfaBackupCode.used_at
    already does for backup codes."""
    _login_and_change_password(client, session)
    secret, _ = _enroll_and_confirm(client)
    client.post("/auth/logout")

    code = _next_totp_code(secret)
    first = _login(client, password="new-password!", mfa_code=code)
    assert first.status_code == 200
    client.post("/auth/logout")

    second = _login(client, password="new-password!", mfa_code=code)
    assert second.status_code == 401
    assert second.json()["detail"]["error_code"] == "mfa_invalid_code"


def test_login_failure_before_mfa_step_does_not_finalize_or_audit_login(client, session):
    """A wrong password should never reach the MFA branch at all -- this
    guards against a regression where the login endpoint's restructuring
    accidentally lets a bad password fall through."""
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)
    client.post("/auth/logout")

    response = _login(client, password="totally-wrong")
    assert response.status_code == 401

    actions = [e.action for e in session.exec(select(AuditLog)).all()]
    assert actions.count("user.login") == 1  # only from _login_and_change_password's own login


def test_login_without_mfa_enabled_ignores_an_extraneous_mfa_code(client, session):
    _make_user(session)
    response = _login(client, mfa_code="123456")
    assert response.status_code == 200


# --- disable -----------------------------------------------------------------


def test_mfa_disable_requires_authentication(client):
    response = client.post("/auth/mfa/disable", json={"password": PASSWORD})
    assert response.status_code == 401


def test_mfa_disable_requires_correct_password(client, session):
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)

    response = client.post("/auth/mfa/disable", json={"password": "wrong"})

    assert response.status_code == 401
    me = client.get("/auth/me")
    assert me.json()["mfa_enabled"] is True


def test_mfa_disable_password_check_is_throttled(client, session, monkeypatch):
    """Reuses the same per-username lockout as /auth/login (see
    app.login_throttle) -- a stolen session token shouldn't let an attacker
    brute-force the account password through this endpoint at unthrottled
    argon2-timing speed."""
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)

    for _ in range(3):
        response = client.post("/auth/mfa/disable", json={"password": "wrong"})
        assert response.status_code == 401

    locked = client.post("/auth/mfa/disable", json={"password": "new-password!"})
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers

    get_settings.cache_clear()


def test_mfa_disable_turns_off_mfa_and_allows_login_without_a_code(client, session):
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)

    response = client.post("/auth/mfa/disable", json={"password": "new-password!"})
    assert response.status_code == 200
    assert response.json()["mfa_enabled"] is False

    client.post("/auth/logout")
    relogin = _login(client, password="new-password!")
    assert relogin.status_code == 200


def test_mfa_disable_audits(client, session):
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)

    client.post("/auth/mfa/disable", json={"password": "new-password!"})

    actions = [e.action for e in session.exec(select(AuditLog)).all()]
    assert "user.mfa_disable" in actions


# --- regenerate backup codes --------------------------------------------------


def test_regenerate_backup_codes_requires_correct_password(client, session):
    _login_and_change_password(client, session)
    _enroll_and_confirm(client)

    response = client.post("/auth/mfa/backup-codes/regenerate", json={"password": "wrong"})
    assert response.status_code == 401


def test_regenerate_backup_codes_returns_a_fresh_set_and_invalidates_the_old_one(client, session):
    _login_and_change_password(client, session)
    secret, old_codes = _enroll_and_confirm(client)

    response = client.post("/auth/mfa/backup-codes/regenerate", json={"password": "new-password!"})
    assert response.status_code == 200
    new_codes = response.json()["backup_codes"]
    assert set(new_codes).isdisjoint(old_codes)

    client.post("/auth/logout")
    old_code_login = _login(client, password="new-password!", mfa_code=old_codes[0])
    assert old_code_login.status_code == 401

    new_code_login = _login(client, password="new-password!", mfa_code=new_codes[0])
    assert new_code_login.status_code == 200


def test_regenerate_backup_codes_requires_mfa_enabled(client, session):
    _login_and_change_password(client, session)

    response = client.post("/auth/mfa/backup-codes/regenerate", json={"password": "new-password!"})
    assert response.status_code == 400
