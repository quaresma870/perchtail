"""Settings -> Email (app/api/smtp_settings.py): super admins only, the
password write-only, every change and test audited without it."""

import json
import socket
import uuid

import pytest
from app import email_delivery
from app.api import smtp_settings
from app.api.auth import session_cookie_name
from app.auth.models import AuditLog, GlobalCapability, Role, User
from app.auth.sessions import create_session
from app.crypto import encrypt_secret
from app.db import get_session
from app.email_delivery import EmailDeliveryError
from app.main import app
from app.models import SmtpSettings
from app.timeutils import utcnow
from fastapi.testclient import TestClient
from sqlmodel import select
from structlog.testing import capture_logs

from tests.smtp_server import (
    REAL_GETADDRINFO,
    SERVER_NAME,
    ServerOptions,
    SmtpTestServer,
    make_ca,
    make_server_certificates,
)

PASSWORD = "very-secret-smtp-password"

SETTINGS = {
    "host": "smtp.example.test",
    "port": 587,
    "security": "starttls",
    "username": "relay",
    "password": PASSWORD,
    "from_address": "PerchTail@Example.test",
    "from_name": "PerchTail",
    "enabled": True,
}


@pytest.fixture(autouse=True)
def _reset_test_limit():
    smtp_settings.reset_test_limit_for_tests()
    yield
    smtp_settings.reset_test_limit_for_tests()


@pytest.fixture()
def real_app(session):
    app.dependency_overrides[get_session] = lambda: session
    yield
    app.dependency_overrides.pop(get_session, None)


def _user(session, *, super_admin=False, capabilities=None, email=None) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=super_admin,
        global_capabilities=capabilities or [],
    )
    session.add(role)
    session.flush()
    user = User(username=f"user-{uuid.uuid4().hex}", role_id=role.id, email=email)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _client(session, user) -> TestClient:
    _, token = create_session(session, user)
    client = TestClient(app)
    client.cookies.set(session_cookie_name(), token)
    return client


@pytest.fixture()
def admin(session):
    return _user(session, super_admin=True, email="admin@example.test")


@pytest.fixture()
def client(session, real_app, admin):
    return _client(session, admin)


def _entries(session, prefix="smtp_settings.") -> list[AuditLog]:
    session.expire_all()
    rows = session.exec(select(AuditLog).order_by(AuditLog.id)).all()
    return [row for row in rows if row.action.startswith(prefix)]


def _row(session) -> SmtpSettings | None:
    session.expire_all()
    return session.exec(select(SmtpSettings)).first()


# --- who may ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/smtp-settings", None),
        ("PUT", "/smtp-settings", SETTINGS),
        ("DELETE", "/smtp-settings", None),
        ("POST", "/smtp-settings/test", {"recipient": "x@example.test"}),
    ],
)
def test_only_super_admins_and_refusals_are_audited(session, real_app, method, path, body):
    user = _user(session, capabilities=[GlobalCapability.manage_system_settings])
    response = _client(session, user).request(method, path, json=body)
    assert response.status_code == 403
    assert PASSWORD not in response.text

    [denied] = _entries(session, "access.denied")
    assert denied.user_id == user.id
    assert denied.event_metadata["attempted"] == f"{method} {path}"
    assert denied.event_metadata["reason"] == "super_admin_required"
    assert _row(session) is None


# --- reading and saving ---------------------------------------------------------


def test_nothing_configured(client):
    response = client.get("/smtp-settings")
    assert response.status_code == 200
    assert response.json() is None


def test_create_never_returns_or_stores_the_password_in_clear(session, client):
    response = client.put("/smtp-settings", json=SETTINGS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "password" not in body
    assert body["password_set"] is True
    assert body["from_address"] == "perchtail@example.test"
    assert body["enabled"] is True
    assert PASSWORD not in client.get("/smtp-settings").text

    row = _row(session)
    assert row.password_encrypted and PASSWORD not in row.password_encrypted

    [created] = _entries(session)
    assert created.action == "smtp_settings.create"
    changes = created.event_metadata["changes"]
    assert changes["password"] == "changed"
    assert changes["host"] == {"from": None, "to": "smtp.example.test"}
    assert changes["enabled"] == {"from": None, "to": True}
    assert PASSWORD not in json.dumps(created.event_metadata)


def test_saving_without_a_password_keeps_it(session, client):
    client.put("/smtp-settings", json=SETTINGS)
    before = _row(session).password_encrypted
    update = {**SETTINGS, "port": 2525, "enabled": False}
    del update["password"]

    response = client.put("/smtp-settings", json=update)
    assert response.status_code == 200
    assert response.json()["password_set"] is True
    assert _row(session).password_encrypted == before

    updated = _entries(session)[-1]
    assert updated.action == "smtp_settings.update"
    assert updated.event_metadata["changes"] == {
        "port": {"from": 587, "to": 2525},
        "enabled": {"from": True, "to": False},
    }


def test_a_new_password_is_audited_as_changed_only(session, client):
    client.put("/smtp-settings", json=SETTINGS)
    client.put("/smtp-settings", json={**SETTINGS, "password": "another-password"})
    updated = _entries(session)[-1]
    assert updated.event_metadata["changes"] == {"password": "changed"}
    assert "another-password" not in json.dumps(updated.event_metadata)


def test_an_empty_password_removes_it(session, client):
    client.put("/smtp-settings", json=SETTINGS)
    response = client.put("/smtp-settings", json={**SETTINGS, "password": ""})
    assert response.json()["password_set"] is False
    assert _row(session).password_encrypted is None
    assert _entries(session)[-1].event_metadata["changes"] == {"password": "removed"}


def test_no_username_means_no_saved_password(session, client):
    client.put("/smtp-settings", json=SETTINGS)
    update = {**SETTINGS, "username": ""}
    del update["password"]
    response = client.put("/smtp-settings", json=update)
    assert response.status_code == 200
    assert response.json()["username"] is None
    assert response.json()["password_set"] is False
    assert _row(session).password_encrypted is None


def test_a_password_without_a_username_is_refused(client):
    response = client.put("/smtp-settings", json={**SETTINGS, "username": None})
    assert response.status_code == 422
    assert response.json()["detail"] == "A password needs a username"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"port": 0}, "port"),
        ({"port": 65536}, "port"),
        ({"from_address": "not-an-address"}, "sender"),
        ({"from_address": ""}, "sender"),
        ({"from_name": "Perch\r\nBcc: x@example.test"}, "control characters"),
        ({"username": "relay\r\nRSET"}, "control characters"),
        ({"host": ""}, "host"),
        ({"host": "smtp://smtp.example.test"}, "host name or an IP address"),
        ({"host": "127.0.0.1"}, "disallowed address"),
        ({"host": "::1"}, "disallowed address"),
        ({"host": "169.254.169.254"}, "disallowed address"),
        ({"host": "224.0.0.1"}, "disallowed address"),
        ({"host": "0.0.0.0"}, "disallowed address"),
        ({"host": "240.0.0.1"}, "disallowed address"),
        ({"tls_ca_pem": "not a certificate"}, "CA certificate"),
        ({"username": "rélay"}, "ASCII characters only"),
        ({"password": "Señha-relay-2026"}, "ASCII characters only"),
    ],
)
def test_invalid_settings_are_refused(session, client, change, message):
    response = client.put("/smtp-settings", json={**SETTINGS, **change})
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert _row(session) is None
    assert _entries(session) == []


@pytest.mark.parametrize("host", ["10.0.0.25", "192.168.1.10", "fd00::25", "relay.internal"])
def test_private_relays_are_accepted(client, host):
    response = client.put("/smtp-settings", json={**SETTINGS, "host": host})
    assert response.status_code == 200, response.text
    assert response.json()["host"] == host


def test_a_host_name_resolving_to_loopback_is_refused(client, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    response = client.put("/smtp-settings", json=SETTINGS)
    assert response.status_code == 422
    assert "disallowed address" in response.json()["detail"]


def test_a_ca_certificate_is_kept_and_its_change_audited(session, client):
    from cryptography.hazmat.primitives import serialization

    _, cert = make_ca()
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    response = client.put("/smtp-settings", json={**SETTINGS, "tls_ca_pem": pem})
    assert response.status_code == 200, response.text
    assert response.json()["tls_ca_pem"].strip() == pem.strip()
    assert _entries(session)[-1].event_metadata["changes"]["tls_ca_pem"] == "replaced"

    client.put("/smtp-settings", json={**SETTINGS, "tls_ca_pem": ""})
    assert _row(session).tls_ca_pem is None
    assert _entries(session)[-1].event_metadata["changes"]["tls_ca_pem"] == "removed"


def test_delete(session, client):
    client.put("/smtp-settings", json=SETTINGS)
    assert client.delete("/smtp-settings").status_code == 204
    assert _row(session) is None
    deleted = _entries(session)[-1]
    assert deleted.action == "smtp_settings.delete"
    assert deleted.event_metadata["before"]["host"] == "smtp.example.test"
    assert PASSWORD not in json.dumps(deleted.event_metadata)
    assert client.delete("/smtp-settings").status_code == 404


# --- the test message -------------------------------------------------------------


@pytest.fixture()
def sent(monkeypatch):
    """SMTP replaced: records each send_email call instead of connecting."""
    calls = []

    def fake_send(session, to, subject, body_text):
        calls.append((to, subject, body_text))

    monkeypatch.setattr(smtp_settings, "send_email", fake_send)
    return calls


def test_the_test_goes_to_the_callers_own_email_by_default(session, client, sent):
    client.put("/smtp-settings", json=SETTINGS)
    response = client.post("/smtp-settings/test")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "detail": "Test message sent to admin@example.test"}
    assert sent == [
        ("admin@example.test", "Test message from PerchTail", "Test message from PerchTail")
    ]

    tested = _entries(session)[-1]
    assert tested.action == "smtp_settings.test"
    assert tested.event_metadata["recipient"] == "admin@example.test"
    assert tested.event_metadata["result"] == "sent"


def test_the_test_to_another_recipient(session, client, sent):
    client.put("/smtp-settings", json=SETTINGS)
    response = client.post("/smtp-settings/test", json={"recipient": "Ops@Example.test"})
    assert response.json()["ok"] is True
    assert sent[0][0] == "ops@example.test"


def test_a_failed_test_is_audited_with_the_error(session, client, monkeypatch):
    def failing(session, to, subject, body_text):
        raise EmailDeliveryError("The SMTP server refused the recipient")

    monkeypatch.setattr(smtp_settings, "send_email", failing)
    client.put("/smtp-settings", json=SETTINGS)
    response = client.post("/smtp-settings/test", json={"recipient": "ops@example.test"})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "detail": "The SMTP server refused the recipient"}
    tested = _entries(session)[-1]
    assert tested.event_metadata == {
        "recipient": "ops@example.test",
        "result": "failed",
        "error": "The SMTP server refused the recipient",
    }


def test_a_test_without_configuration(session, client):
    response = client.post("/smtp-settings/test")
    assert response.json() == {"ok": False, "detail": "Email isn't configured"}
    assert _entries(session)[-1].event_metadata["result"] == "not_configured"


def test_a_test_needs_a_recipient(session, real_app):
    admin = _user(session, super_admin=True)
    response = _client(session, admin).post("/smtp-settings/test")
    assert response.status_code == 422
    assert "recipient" in response.json()["detail"]


def test_tests_are_limited_per_installation(session, client, sent):
    client.put("/smtp-settings", json=SETTINGS)
    for _ in range(smtp_settings.TEST_SENDS_PER_MINUTE):
        assert client.post("/smtp-settings/test").status_code == 200
    response = client.post("/smtp-settings/test")
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert len(sent) == smtp_settings.TEST_SENDS_PER_MINUTE
    assert _entries(session)[-1].event_metadata["result"] == "rate_limited"


def test_a_host_that_resolves_elsewhere_after_saving_is_checked_again(session, client, monkeypatch):
    assert client.put("/smtp-settings", json=SETTINGS).status_code == 200

    # The name now points at the cloud metadata address: no connection is made.
    def refuse(*args, **kwargs):
        raise AssertionError("no connection may be opened")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))],
    )
    response = client.post("/smtp-settings/test")
    assert response.json()["ok"] is False
    assert "disallowed address" in response.json()["detail"]
    assert _entries(session)[-1].event_metadata["result"] == "failed"


def test_a_saved_non_ascii_password_fails_the_test_and_is_audited(
    session, client, monkeypatch, tmp_path
):
    # Saved before the API refused it: the real send runs, fails with a
    # fixed message, and the test is answered and audited like any failure.
    password = "Señha-relay-2026"
    certificates = make_server_certificates(tmp_path / "certs")
    monkeypatch.setattr(socket, "getaddrinfo", REAL_GETADDRINFO)
    monkeypatch.setattr(email_delivery, "resolve_validated_smtp_ips", lambda host: ["127.0.0.1"])
    options = ServerOptions(certificates=certificates, password=password)
    with capture_logs() as logs:
        with SmtpTestServer(options) as server:
            session.add(
                SmtpSettings(
                    host=SERVER_NAME,
                    port=server.port,
                    username="relay",
                    password_encrypted=encrypt_secret(password),
                    from_address="perchtail@example.test",
                    tls_ca_pem=certificates.ca_pem,
                    enabled=True,
                    updated_at=utcnow(),
                )
            )
            session.commit()
            response = client.post("/smtp-settings/test", json={"recipient": "ops@example.test"})

    assert response.status_code == 200
    assert response.json() == {"ok": False, "detail": email_delivery.NON_ASCII_CREDENTIALS}
    assert options.received == []
    assert _entries(session)[-1].event_metadata == {
        "recipient": "ops@example.test",
        "result": "failed",
        "error": email_delivery.NON_ASCII_CREDENTIALS,
    }
    text = repr(logs)
    for leaked in (password, "ñ", "\\xf1", "position"):
        assert leaked not in text


def test_the_password_never_reaches_the_log_or_the_audit_log(session, client, sent):
    with capture_logs() as logs:
        client.put("/smtp-settings", json=SETTINGS)
        client.put("/smtp-settings", json={**SETTINGS, "password": "second-" + PASSWORD})
        client.post("/smtp-settings/test")
        client.delete("/smtp-settings")
    assert PASSWORD not in repr(logs)
    assert all(PASSWORD not in json.dumps(e.event_metadata) for e in _entries(session))
