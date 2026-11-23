"""Sending email through the system SMTP server (app/email_delivery.py),
against a local test server with real TLS (tests/smtp_server.py). The
server's name is mail.example.test; the address check is replaced so the
client is pinned to 127.0.0.1, which also shows the connection goes to the
checked IP while the certificate is verified against the name."""

import socket

import pytest
from app import email_delivery
from app.crypto import encrypt_secret
from app.email_delivery import EmailDeliveryError, EmailNotConfiguredError, send_email
from app.models import SmtpSettings
from app.timeutils import utcnow
from structlog.testing import capture_logs

from tests.smtp_server import (
    REAL_GETADDRINFO,
    SERVER_NAME,
    ServerOptions,
    SmtpTestServer,
    make_server_certificates,
)

PASSWORD = "relay-password"
BODY = "The body nobody should log"


@pytest.fixture()
def resolved(monkeypatch):
    """Real name resolution for 127.0.0.1, and the SMTP address check
    answering 127.0.0.1 for the server's name. Records what it was asked."""
    monkeypatch.setattr(socket, "getaddrinfo", REAL_GETADDRINFO)
    asked = []

    def fake_resolve(hostname):
        asked.append(hostname)
        return ["127.0.0.1"]

    monkeypatch.setattr(email_delivery, "resolve_validated_smtp_ips", fake_resolve)
    return asked


@pytest.fixture()
def certificates(tmp_path):
    return make_server_certificates(tmp_path / "certs")


def _configure(session, port, *, ca_pem=None, security="starttls", username="relay"):
    row = SmtpSettings(
        host=SERVER_NAME,
        port=port,
        security=security,
        username=username,
        password_encrypted=encrypt_secret(PASSWORD) if username else None,
        from_address="perchtail@example.test",
        from_name="PerchTail",
        tls_ca_pem=ca_pem,
        enabled=True,
        updated_at=utcnow(),
    )
    session.add(row)
    session.commit()
    return row


def test_starttls_sends_one_plain_text_message_after_tls(session, resolved, certificates):
    options = ServerOptions(certificates=certificates)
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=certificates.ca_pem)
        send_email(session, "Alice@Example.test", "Hello", BODY)

    assert resolved == [SERVER_NAME]
    [received] = options.received
    assert received.tls is True
    assert received.auth == ("relay", PASSWORD)
    assert received.rcpt_to == ["<alice@example.test>"]
    assert received.mail_from == "<perchtail@example.test>"
    data = received.data.decode()
    assert "Auto-Submitted: auto-generated" in data
    assert "Content-Type: text/plain" in data
    assert "Subject: Hello" in data
    assert "From: PerchTail <perchtail@example.test>" in data
    assert BODY in data


def test_implicit_tls(session, resolved, certificates):
    options = ServerOptions(certificates=certificates, implicit_tls=True, offer_starttls=False)
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=certificates.ca_pem, security="tls")
        send_email(session, "alice@example.test", "Hello", BODY)

    [received] = options.received
    assert received.tls is True
    assert received.auth == ("relay", PASSWORD)


def test_a_relay_without_authentication(session, resolved, certificates):
    options = ServerOptions(certificates=certificates, offer_auth=False)
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=certificates.ca_pem, username=None)
        send_email(session, "alice@example.test", "Hello", BODY)

    [received] = options.received
    assert received.tls is True
    assert received.auth is None


def test_a_server_without_starttls_fails_and_nothing_goes_in_plain_text(
    session, resolved, certificates
):
    options = ServerOptions(certificates=certificates, offer_starttls=False)
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=certificates.ca_pem)
        with pytest.raises(EmailDeliveryError, match="doesn't offer STARTTLS"):
            send_email(session, "alice@example.test", "Hello", BODY)

    assert options.connections == 1
    assert options.received == []


def test_an_untrusted_certificate_fails(session, resolved, certificates):
    options = ServerOptions(certificates=certificates)
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=None)
        with pytest.raises(EmailDeliveryError, match="certificate couldn't be verified"):
            send_email(session, "alice@example.test", "Hello", BODY)
    assert options.received == []


def test_a_certificate_for_another_host_fails(session, resolved, tmp_path):
    other = make_server_certificates(tmp_path / "other", dns_name="other.example.test")
    options = ServerOptions(certificates=other)
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=other.ca_pem)
        with pytest.raises(EmailDeliveryError, match="certificate couldn't be verified"):
            send_email(session, "alice@example.test", "Hello", BODY)
    assert options.received == []


def test_implicit_tls_also_verifies_the_certificate(session, resolved, certificates):
    options = ServerOptions(certificates=certificates, implicit_tls=True)
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=None, security="tls")
        with pytest.raises(EmailDeliveryError, match="certificate couldn't be verified"):
            send_email(session, "alice@example.test", "Hello", BODY)
    assert options.received == []


def test_a_wrong_password(session, resolved, certificates):
    options = ServerOptions(certificates=certificates, password="something-else")
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=certificates.ca_pem)
        with pytest.raises(EmailDeliveryError, match="refused the username or password"):
            send_email(session, "alice@example.test", "Hello", BODY)
    assert options.received == []


def test_a_refused_recipient(session, resolved, certificates):
    options = ServerOptions(certificates=certificates, refuse_recipient=True)
    with SmtpTestServer(options) as server:
        _configure(session, server.port, ca_pem=certificates.ca_pem)
        with pytest.raises(EmailDeliveryError, match="refused the recipient"):
            send_email(session, "alice@example.test", "Hello", BODY)


def test_nothing_listening(session, resolved, certificates):
    with SmtpTestServer(ServerOptions(certificates=certificates)) as server:
        port = server.port
    _configure(session, port, ca_pem=certificates.ca_pem)
    with pytest.raises(EmailDeliveryError, match="Couldn't connect"):
        send_email(session, "alice@example.test", "Hello", BODY)


NON_ASCII_PASSWORD = "Señha-relay-2026"


@pytest.mark.parametrize(
    ("username", "password"),
    [("relay", NON_ASCII_PASSWORD), ("rélay", PASSWORD)],
)
def test_non_ascii_credentials_fail_the_send_without_leaking_them(
    session, resolved, certificates, username, password
):
    # Saved before the API refused them (or written some other way): the
    # send fails with a fixed message, nothing escapes the error handling,
    # and neither the credentials nor the encoder's message are logged.
    options = ServerOptions(certificates=certificates, password=password)
    with capture_logs() as logs:
        with SmtpTestServer(options) as server:
            row = _configure(session, server.port, ca_pem=certificates.ca_pem)
            row.username = username
            row.password_encrypted = encrypt_secret(password)
            session.add(row)
            session.commit()
            with pytest.raises(EmailDeliveryError) as raised:
                send_email(session, "alice@example.test", "Hello", BODY)

    assert str(raised.value) == email_delivery.NON_ASCII_CREDENTIALS
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert options.received == []
    text = repr(logs)
    for leaked in (password, username, "ascii", "ñ", "é", "\\xf1", "\\xe9", "position"):
        assert leaked not in text
    assert [entry["error"] for entry in logs if entry["event"] == "email.failed"] == [
        email_delivery.NON_ASCII_CREDENTIALS
    ]


def test_any_other_error_while_authenticating_is_a_failed_send(
    session, resolved, certificates, monkeypatch
):
    def broken_login(self, user, password, **kwargs):
        raise ValueError(f"unexpected: {password}")

    monkeypatch.setattr(email_delivery.smtplib.SMTP, "login", broken_login)
    options = ServerOptions(certificates=certificates)
    with capture_logs() as logs:
        with SmtpTestServer(options) as server:
            _configure(session, server.port, ca_pem=certificates.ca_pem)
            with pytest.raises(EmailDeliveryError) as raised:
                send_email(session, "alice@example.test", "Hello", BODY)

    assert str(raised.value) == "Authentication with the SMTP server failed"
    assert raised.value.__cause__ is None
    assert PASSWORD not in repr(logs)
    assert options.received == []


def _no_connections(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("no connection may be opened")

    monkeypatch.setattr(socket, "create_connection", refuse)


def test_without_configuration_nothing_is_sent(session, monkeypatch):
    _no_connections(monkeypatch)
    with pytest.raises(EmailNotConfiguredError, match="isn't configured"):
        send_email(session, "alice@example.test", "Hello", BODY)


def test_turned_off_nothing_is_sent(session, monkeypatch):
    _no_connections(monkeypatch)
    row = _configure(session, 587)
    row.enabled = False
    session.add(row)
    session.commit()
    with pytest.raises(EmailNotConfiguredError, match="turned off"):
        send_email(session, "alice@example.test", "Hello", BODY)


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"])
def test_a_host_resolving_to_a_refused_address_is_checked_at_send(session, monkeypatch, address):
    _no_connections(monkeypatch)

    def resolve(host, *args, **kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    _configure(session, 587)
    with pytest.raises(EmailDeliveryError, match="disallowed address"):
        send_email(session, "alice@example.test", "Hello", BODY)


def test_an_invalid_recipient_is_refused_before_connecting(session, monkeypatch):
    _no_connections(monkeypatch)
    _configure(session, 587)
    with pytest.raises(EmailDeliveryError, match="recipient"):
        send_email(session, "not an address\r\nBcc: x@example.test", "Hello", BODY)


def test_the_password_and_the_message_are_never_logged(session, resolved, certificates):
    good = ServerOptions(certificates=certificates)
    with capture_logs() as logs:
        with SmtpTestServer(good) as server:
            row = _configure(session, server.port, ca_pem=certificates.ca_pem)
            send_email(session, "alice@example.test", "Hello", BODY)
        bad = ServerOptions(certificates=certificates, password="other")
        with SmtpTestServer(bad) as server:
            row.port = server.port
            session.add(row)
            session.commit()
            with pytest.raises(EmailDeliveryError) as raised:
                send_email(session, "alice@example.test", "Hello", BODY)

    text = repr(logs)
    assert {entry["event"] for entry in logs} >= {"email.sent", "email.failed"}
    for secret in (PASSWORD, BODY):
        assert secret not in text
        assert secret not in str(raised.value)
