"""Password recovery by email for local accounts (#244): app/auth/password_reset.py,
the /auth/password-reset/* and /auth/login-options routes, and the settings
that turn it on."""

import base64
import json
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app import password_reset_limits, system_settings
from app.api import auth as auth_api
from app.api.auth import session_cookie_name
from app.auth import password_reset
from app.auth.models import (
    AuditLog,
    AuthProviderType,
    AuthSession,
    GlobalCapability,
    PasswordHistory,
    PasswordResetToken,
    Role,
    User,
)
from app.auth.providers.local import hash_password, set_password, verify_password
from app.auth.sessions import create_session, hash_token
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import SmtpSettings
from app.timeutils import utcnow
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlmodel import select
from structlog.testing import capture_logs

PASSWORD = "Old-Lantern-Orbit-2026"
NEW_PASSWORD = "Fresh-Kettle-Harbor-2031"
BASE_URL = "https://perchtail.example"
LINK = re.compile(re.escape(BASE_URL) + r"/#/reset-password/([A-Za-z0-9_-]+)")


@pytest.fixture(autouse=True)
def _reset_limits():
    password_reset_limits.reset_for_tests()
    yield
    password_reset_limits.reset_for_tests()


@pytest.fixture()
def real_app(session):
    app.dependency_overrides[get_session] = lambda: session
    yield
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture()
def outbox(monkeypatch) -> list[dict]:
    """The emails password_reset sends, instead of an SMTP server."""
    sent: list[dict] = []

    def fake_send(_session, to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(password_reset, "send_email", fake_send)
    return sent


def _email_on(session, *, enabled=True) -> None:
    session.add(
        SmtpSettings(
            host="smtp.example.test",
            port=587,
            from_address="perchtail@example.test",
            enabled=enabled,
            updated_at=utcnow(),
        )
    )
    session.commit()


@pytest.fixture()
def https_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", BASE_URL)


@pytest.fixture()
def recovery_on(session, real_app, https_url, outbox):
    _email_on(session)
    system_settings.set_bool(session, "password_reset_enabled", True)
    session.commit()
    return outbox


@pytest.fixture()
def client(real_app) -> TestClient:
    return TestClient(app)


def _role(session, **kwargs) -> Role:
    role = Role(name=f"role-{uuid.uuid4().hex}", **kwargs)
    session.add(role)
    session.commit()
    return role


def _user(session, username="jdoe", email="jdoe@example.com", **kwargs) -> User:
    role = kwargs.pop("role", None) or _role(session)
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(PASSWORD),
        role_id=role.id,
        password_changed_at=utcnow(),
        **kwargs,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _events(session, action: str) -> list[AuditLog]:
    session.expire_all()
    rows = session.exec(select(AuditLog).order_by(AuditLog.id)).all()
    return [row for row in rows if row.action == action]


def _ask(client, identifier="jdoe"):
    return client.post("/auth/password-reset/request", json={"identifier": identifier})


def _token_from(outbox) -> str:
    match = LINK.search(outbox[-1]["body"])
    assert match, outbox[-1]["body"]
    return match.group(1)


def _complete(client, token, password=NEW_PASSWORD):
    return client.post(
        "/auth/password-reset/complete", json={"token": token, "new_password": password}
    )


# --- off unless everything it needs is there ----------------------------------


@pytest.mark.parametrize("missing", ["setting", "email", "email_off", "https"])
def test_off_or_unusable_hides_the_link_and_the_routes(
    session, real_app, monkeypatch, outbox, missing
):
    if missing != "https":
        monkeypatch.setattr(get_settings(), "public_base_url", BASE_URL)
    if missing != "email":
        _email_on(session, enabled=missing != "email_off")
    system_settings.set_bool(session, "password_reset_enabled", missing != "setting")
    session.commit()
    _user(session)
    client = TestClient(app)

    assert client.get("/auth/login-options").json() == {"password_reset": False}
    for path, body in (
        ("/auth/password-reset/request", {"identifier": "jdoe"}),
        ("/auth/password-reset/check", {"token": "x"}),
        ("/auth/password-reset/complete", {"token": "x", "new_password": NEW_PASSWORD}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"
    assert outbox == []


def test_on_shows_the_link(session, recovery_on, client):
    response = client.get("/auth/login-options")
    assert response.json() == {"password_reset": True}


# --- asking for a link ---------------------------------------------------------


def test_the_answer_is_the_same_whoever_asks_and_only_eligible_accounts_get_mail(
    session, recovery_on, client
):
    outbox = recovery_on
    _user(session)
    _user(session, username="gone", email="gone@example.com", active=False)
    _user(session, username="noemail", email=None)
    _user(
        session,
        username="sso",
        email="sso@example.com",
        auth_provider=AuthProviderType.oidc,
        external_issuer="https://idp.example",
        external_id="sub-1",
    )
    _user(
        session, username="root", email="root@example.com", role=_role(session, is_super_admin=True)
    )
    _user(
        session,
        username="auditor",
        email="auditor@example.com",
        role=_role(session, global_capabilities=[GlobalCapability.view_audit_log]),
    )

    answers = {
        identifier: _ask(client, identifier)
        for identifier in ("jdoe", "nobody", "gone", "noemail", "sso", "root", "auditor")
    }
    for response in answers.values():
        assert response.status_code == 202
        assert response.json() == {"status": "accepted"}
        assert response.headers["cache-control"] == "no-store"

    assert [mail["to"] for mail in outbox] == ["jdoe@example.com"]
    assert outbox[0]["subject"] == "Reset your PerchTail password"

    results = {
        (row.event_metadata.get("username") or row.event_metadata.get("identifier")): (
            row.event_metadata["result"],
            row.event_metadata.get("reason"),
        )
        for row in _events(session, "user.password_reset_requested")
    }
    assert results == {
        "jdoe": ("sent", None),
        "nobody": ("no_match", None),
        "gone": ("not_eligible", "inactive"),
        "noemail": ("not_eligible", "no_email"),
        "sso": ("not_eligible", "sso_account"),
        "root": ("not_eligible", "privileged"),
        "auditor": ("not_eligible", "privileged"),
    }


def test_an_email_address_finds_the_account_too(session, recovery_on, client):
    _user(session)
    assert _ask(client, " JDoe@Example.com ").status_code == 202
    assert [mail["to"] for mail in recovery_on] == ["jdoe@example.com"]


def test_the_handler_only_schedules_the_work(session, recovery_on):
    """The response doesn't wait for the lookup or the SMTP server, so its
    timing doesn't say whether the account exists."""
    _user(session)
    background = BackgroundTasks()
    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))

    answer = auth_api.request_password_reset(
        auth_api.PasswordResetRequest(identifier="jdoe"), request, background, session
    )

    assert answer.status == "accepted"
    assert recovery_on == []
    assert session.exec(select(PasswordResetToken)).all() == []
    (task,) = background.tasks
    task.func(*task.args, **task.kwargs)
    assert len(recovery_on) == 1


def test_a_malformed_identifier_is_refused(session, recovery_on, client):
    for identifier in ("", "   ", "a\nb", "x" * 255):
        response = _ask(client, identifier)
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"


def test_the_email_holds_only_the_link_and_its_lifetime(session, recovery_on, client):
    system_settings.set_int(session, "password_reset_minutes", 15)
    session.commit()
    _user(session, display_name="Jo Doe")
    _ask(client)

    body = recovery_on[0]["body"]
    assert "15 minutes" in body
    assert "ignore this email" in body
    for private in ("jdoe@", "Jo Doe", "testclient", "203.0.113"):
        assert private not in body
    assert body.count("jdoe") == 0


def test_the_token_is_random_and_only_its_hash_is_stored(session, recovery_on, client):
    user = _user(session)
    _ask(client)
    token = _token_from(recovery_on)

    assert len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))) >= 32
    (row,) = session.exec(select(PasswordResetToken)).all()
    assert row.user_id == user.id
    assert row.token_hash == hash_token(token)
    assert token not in json.dumps(row.model_dump(), default=str)
    assert row.expires_at - row.created_at == timedelta(minutes=30)


def test_a_failed_send_drops_the_link_and_is_audited(session, recovery_on, client, monkeypatch):
    from app.email_delivery import EmailDeliveryError

    def broken(*_args):
        raise EmailDeliveryError("Couldn't connect to the SMTP server")

    monkeypatch.setattr(password_reset, "send_email", broken)
    _user(session)
    assert _ask(client).status_code == 202
    session.expire_all()
    assert session.exec(select(PasswordResetToken)).all() == []
    (event,) = _events(session, "user.password_reset_requested")
    assert event.event_metadata["result"] == "send_failed"


# --- using the link ------------------------------------------------------------


def test_a_password_over_the_policy_maximum_is_refused_before_anything_else(
    session, recovery_on, client, monkeypatch
):
    user = _user(session)
    hash_before = user.password_hash
    _ask(client)
    token = _token_from(recovery_on)
    monkeypatch.setattr(
        "app.auth.password_reset.complete_reset", lambda *a, **k: pytest.fail("no hashing")
    )

    response = _complete(client, token, "x" * 129)

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "password_too_long"
    assert "x" * 129 not in response.text
    session.expire_all()
    assert session.get(User, user.id).password_hash == hash_before


def test_the_link_sets_the_password_once_and_ends_every_session(session, recovery_on, client):
    user = _user(session, must_change_password=True)
    create_session(session, user)
    create_session(session, user)
    _ask(client)
    token = _token_from(recovery_on)

    check = client.post("/auth/password-reset/check", json={"token": token})
    assert check.status_code == 200
    assert check.headers["cache-control"] == "no-store"
    assert check.json()["valid"] is True
    assert check.json()["policy"]["min_length"] == 12

    response = _complete(client, token)
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers

    session.expire_all()
    user = session.get(User, user.id)
    assert verify_password(user, NEW_PASSWORD)
    assert user.must_change_password is False
    assert session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).all() == []
    assert session.exec(select(PasswordResetToken)).all() == []
    (done,) = _events(session, "user.password_reset_completed")
    assert done.target_id == user.id
    (revoked,) = _events(session, "session.revoke")
    assert revoked.event_metadata["reason"] == "password_reset"
    assert revoked.event_metadata["count"] == 2

    again = _complete(client, token, "Another-Orchard-Pylon-2032")
    assert again.status_code == 400
    assert again.json()["detail"]["error_code"] == "reset_link_invalid"
    assert client.post("/auth/password-reset/check", json={"token": token}).json() == {
        "valid": False,
        "policy": None,
    }
    (failed,) = _events(session, "user.password_reset_failed")
    assert failed.event_metadata["reason"] == "invalid_token"


def test_the_next_sign_in_still_asks_for_the_second_factor(session, recovery_on, client):
    user = _user(session)
    user.mfa_enabled = True
    session.add(user)
    session.commit()
    _ask(client)
    assert _complete(client, _token_from(recovery_on)).status_code == 204
    session.expire_all()
    assert session.get(User, user.id).mfa_enabled is True

    login = client.post("/auth/login", json={"username": "jdoe", "password": NEW_PASSWORD})
    assert login.status_code == 401
    assert login.json()["detail"]["error_code"] == "mfa_required"


def test_the_link_expires(session, recovery_on, client):
    _user(session)
    _ask(client)
    token = _token_from(recovery_on)
    row = session.exec(select(PasswordResetToken)).one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    session.add(row)
    session.commit()

    assert _complete(client, token).status_code == 400
    (failed,) = _events(session, "user.password_reset_failed")
    assert failed.event_metadata["reason"] == "expired"
    assert session.exec(select(PasswordResetToken)).all() == []


def test_checking_a_link_that_cant_be_used_is_audited(session, recovery_on, client):
    user = _user(session)
    _ask(client)
    token = _token_from(recovery_on)
    row = session.exec(select(PasswordResetToken)).one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    session.add(row)
    session.commit()

    assert client.post("/auth/password-reset/check", json={"token": token}).json() == {
        "valid": False,
        "policy": None,
    }
    for _ in range(3):
        client.post("/auth/password-reset/check", json={"token": "guess"})

    expired, unknown = _events(session, "user.password_reset_failed")
    assert expired.event_metadata["reason"] == "expired"
    assert expired.target_id == user.id
    assert unknown.event_metadata["reason"] == "invalid_token"
    assert unknown.target_id is None
    # Repeats within the folding window don't flood the chain.
    assert len(_events(session, "user.password_reset_failed")) == 2


def _make_admin(session, user):
    user.role_id = _role(session, is_super_admin=True).id


def _make_capable(session, user):
    user.role_id = _role(session, global_capabilities=[GlobalCapability.manage_alerts]).id


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda s, u: set_password(s, u, "Someone-Else-Chose-2033"), "superseded"),
        (lambda s, u: setattr(u, "email", "other@example.com"), "superseded"),
        (lambda s, u: setattr(u, "active", False), "not_eligible"),
        (lambda s, u: setattr(u, "auth_provider", AuthProviderType.oidc), "not_eligible"),
        (_make_admin, "not_eligible"),
        (_make_capable, "not_eligible"),
    ],
    ids=["password", "email", "deactivated", "sso", "super_admin", "global_capability"],
)
def test_the_link_dies_when_the_account_changes(session, recovery_on, client, change, reason):
    user = _user(session)
    _ask(client)
    token = _token_from(recovery_on)
    session.refresh(user)
    change(session, user)
    session.add(user)
    session.commit()

    assert _complete(client, token).status_code == 400
    (failure,) = _events(session, "user.password_reset_failed")
    assert failure.event_metadata["reason"] == reason
    assert failure.target_id == user.id
    # The link is gone for good.
    assert client.post("/auth/password-reset/check", json={"token": token}).json()["valid"] is False
    assert session.exec(select(PasswordResetToken)).all() == []
    session.expire_all()
    assert not verify_password(session.get(User, user.id), NEW_PASSWORD)


def test_an_admin_reset_kills_the_link(session, recovery_on, client):
    user = _user(session)
    _ask(client)
    token = _token_from(recovery_on)
    admin_role = _role(session, is_super_admin=True)
    admin = _user(session, username="admin", email="admin@example.com", role=admin_role)
    _, admin_token = create_session(session, admin)
    admin_client = TestClient(app)
    admin_client.cookies.set(session_cookie_name(), admin_token)
    assert admin_client.post(f"/users/{user.id}/reset-password").status_code == 200

    assert _complete(client, token).status_code == 400


def test_a_new_link_replaces_the_old_one(session, recovery_on, client):
    _user(session)
    _ask(client)
    first = _token_from(recovery_on)
    _ask(client)
    second = _token_from(recovery_on)
    assert first != second

    assert _complete(client, first).status_code == 400
    assert _complete(client, second).status_code == 204


# --- the password policy ----------------------------------------------------------


def test_the_policy_applies_and_the_link_survives_a_refusal(session, recovery_on, client):
    _user(session)
    _ask(client)
    token = _token_from(recovery_on)

    short = _complete(client, token, "short")
    assert short.status_code == 422
    assert short.headers["cache-control"] == "no-store"
    same = _complete(client, token, PASSWORD)
    assert same.status_code == 422
    assert same.json()["detail"]["error_code"] == "password_same_as_current"
    # The second refusal within a minute is folded into the next entry.
    (refused,) = _events(session, "user.password_reset_failed")
    assert refused.event_metadata["reason"] == "policy"

    assert _complete(client, token).status_code == 204


def test_the_history_rule_applies(session, recovery_on, client):
    system_settings.set_int(session, "password_history", 3)
    session.commit()
    user = _user(session)
    set_password(session, user, NEW_PASSWORD)
    set_password(session, user, PASSWORD)
    session.commit()
    assert session.exec(select(PasswordHistory)).all()
    _ask(client)

    response = _complete(client, _token_from(recovery_on), NEW_PASSWORD)
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "password_reused"


# --- limits ----------------------------------------------------------------------


def test_asking_too_often_from_one_address_is_refused(session, recovery_on, client):
    for index in range(password_reset_limits.REQUESTS_PER_ADDRESS):
        assert _ask(client, f"nobody-{index}").status_code == 202
    response = _ask(client, "nobody-else")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert response.headers["cache-control"] == "no-store"
    limited = [
        e
        for e in _events(session, "user.password_reset_requested")
        if e.event_metadata["result"] == "rate_limited"
    ]
    assert len(limited) == 1
    assert limited[0].event_metadata["identifier"] == "nobody-else"


def test_asking_too_often_for_one_account_sends_nothing_more(session, recovery_on, client):
    _user(session)
    for identifier in ("jdoe", "jdoe@example.com", "jdoe", "JDOE@example.com"):
        assert _ask(client, identifier).status_code == 202
    assert len(recovery_on) == password_reset_limits.REQUESTS_PER_IDENTIFIER
    events = _events(session, "user.password_reset_requested")
    results = [e.event_metadata["result"] for e in events]
    assert results == ["sent", "sent", "sent", "rate_limited"]


def test_trying_tokens_too_often_is_refused(session, recovery_on, client):
    for _ in range(password_reset_limits.TOKEN_ATTEMPTS_PER_ADDRESS):
        assert client.post("/auth/password-reset/check", json={"token": "guess"}).status_code == 200
    response = _complete(client, "guess")
    assert response.status_code == 429
    assert "retry-after" in response.headers
    reasons = [e.event_metadata["reason"] for e in _events(session, "user.password_reset_failed")]
    # The repeated unknown links are folded into the first entry.
    assert reasons == ["invalid_token", "rate_limited"]


def test_a_full_limit_table_refuses_new_addresses(monkeypatch):
    monkeypatch.setattr(password_reset_limits, "_MAX_TRACKED_KEYS", 2)
    assert password_reset_limits.request_from_address("198.51.100.1") is None
    assert password_reset_limits.request_from_address("198.51.100.2") is None
    assert password_reset_limits.request_from_address("198.51.100.3") is not None


def test_completing_doesnt_clear_a_login_lockout(session, recovery_on, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_max_attempts", 2)
    _user(session)
    for _ in range(2):
        client.post("/auth/login", json={"username": "jdoe", "password": "wrong-password-1"})
    _ask(client)
    assert _complete(client, _token_from(recovery_on)).status_code == 204
    locked = client.post("/auth/login", json={"username": "jdoe", "password": NEW_PASSWORD})
    assert locked.status_code == 429


# --- secrets stay out of logs and the audit log ------------------------------------


def test_no_token_link_or_password_in_logs_or_audit(session, recovery_on, client):
    _user(session)
    with capture_logs() as logs:
        _ask(client)
        token = _token_from(recovery_on)
        client.post("/auth/password-reset/check", json={"token": token})
        _complete(client, token, "short")
        _complete(client, token)
        _complete(client, token)

    logged = json.dumps(logs, default=str)
    session.expire_all()
    audited = json.dumps(
        [row.event_metadata for row in session.exec(select(AuditLog)).all()], default=str
    )
    for secret in (token, NEW_PASSWORD, "short", "reset-password/"):
        assert secret not in logged
        assert secret not in audited


# --- the expired-link sweep ---------------------------------------------------------


def test_expired_links_are_purged(session):
    user = _user(session)
    for minutes in (-5, 30):
        session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=uuid.uuid4().hex,
                password_fingerprint="x",
                email_fingerprint="y",
                expires_at=utcnow() + timedelta(minutes=minutes),
            )
        )
    session.commit()
    assert password_reset.purge_expired_tokens(session) == 1
    assert len(session.exec(select(PasswordResetToken)).all()) == 1


# --- the settings ---------------------------------------------------------------------


def _settings_client(session, user) -> TestClient:
    _, token = create_session(session, user)
    client = TestClient(app)
    client.cookies.set(session_cookie_name(), token)
    return client


def test_only_a_super_admin_changes_the_recovery_settings(session, real_app, https_url):
    _email_on(session)
    manager = _user(
        session,
        username="manager",
        email="m@example.com",
        role=_role(session, global_capabilities=[GlobalCapability.manage_system_settings]),
    )
    client = _settings_client(session, manager)
    for patch in ({"password_reset_enabled": True}, {"password_reset_minutes": 10}):
        assert client.patch("/system-settings", json=patch).status_code == 403
    # Sending the current values changes nothing, so it isn't refused.
    assert client.patch("/system-settings", json={"password_reset_minutes": 30}).status_code == 200
    assert system_settings.get_bool(session, "password_reset_enabled") is False


def test_turning_it_on_needs_email_and_https(session, real_app, monkeypatch):
    admin = _user(
        session, username="root", email="root@example.com", role=_role(session, is_super_admin=True)
    )
    client = _settings_client(session, admin)

    response = client.patch("/system-settings", json={"password_reset_enabled": True})
    assert response.status_code == 422
    assert response.json()["detail"] == password_reset.EMAIL_NOT_READY

    _email_on(session)
    response = client.patch("/system-settings", json={"password_reset_enabled": True})
    assert response.status_code == 422
    assert response.json()["detail"] == password_reset.URL_NOT_HTTPS
    assert client.get("/system-settings").json()["password_reset_unavailable"] == (
        password_reset.URL_NOT_HTTPS
    )

    monkeypatch.setattr(get_settings(), "public_base_url", BASE_URL)
    response = client.patch(
        "/system-settings", json={"password_reset_enabled": True, "password_reset_minutes": 10}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["password_reset_enabled"], body["password_reset_minutes"]) == (True, 10)
    assert body["password_reset_unavailable"] is None
    (entry,) = _events(session, "system_settings.update")
    assert entry.event_metadata["password_reset_enabled"] == {"old": False, "new": True}

    for minutes in (4, 61):
        bad = client.patch("/system-settings", json={"password_reset_minutes": minutes})
        assert bad.status_code == 422
