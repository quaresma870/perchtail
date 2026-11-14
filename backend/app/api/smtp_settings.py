"""The system SMTP server (#244): Settings -> Email, super admins only.

The password is write-only: GET says whether one is saved (password_set),
PUT replaces it only when the request carries one ("" removes it), and it's
never logged or audited -- an audit entry only says it "changed" or was
"removed". The host is resolved and checked against the SMTP address policy
when saved (app.webhook_safety.resolve_validated_smtp_ips) and again before
every send (app/email_delivery.py)."""

import math
import re
import ssl
import threading
import time
from collections import deque
from datetime import datetime
from ipaddress import ip_address
from typing import Literal

from cryptography import x509
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from app import profile
from app.api.auth import require_super_admin
from app.audit import record_audit_event
from app.auth.models import User
from app.crypto import encrypt_secret
from app.db import get_session
from app.email_delivery import (
    NON_ASCII_CREDENTIALS,
    EmailDeliveryError,
    EmailNotConfiguredError,
    get_smtp_settings,
    send_email,
)
from app.models import SmtpSettings
from app.timeutils import utcnow
from app.webhook_safety import UnsafeSmtpHostError, resolve_validated_smtp_ips

router = APIRouter(prefix="/smtp-settings", tags=["smtp-settings"])

MAX_HOST = 253
MAX_USERNAME = 256
MAX_PASSWORD = 1024
MAX_CA_PEM = 64 * 1024

# Test messages: at most this many per minute for the whole installation, so
# the button can't be used to flood a mailbox or the relay.
TEST_SENDS_PER_MINUTE = 5
TEST_WINDOW_SECONDS = 60.0
TEST_MESSAGE = "Test message from PerchTail"

_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^{_HOST_LABEL}(?:\.{_HOST_LABEL})*\.?$")

# Non-secret fields: what an audit entry records before and after.
_AUDITED_FIELDS = (
    "host",
    "port",
    "security",
    "username",
    "from_address",
    "from_name",
    "enabled",
)

# What an audit entry says about the password instead of the password.
_CHANGED = "changed"
_REMOVED = "removed"

_test_sends: deque[float] = deque()
_test_lock = threading.Lock()


def reset_test_limit_for_tests() -> None:
    with _test_lock:
        _test_sends.clear()


def _take_test_slot() -> int | None:
    """None when a test send may go now (and counts it); otherwise the
    whole number of seconds until one may, for Retry-After."""
    now = time.monotonic()
    with _test_lock:
        while _test_sends and now - _test_sends[0] >= TEST_WINDOW_SECONDS:
            _test_sends.popleft()
        if len(_test_sends) >= TEST_SENDS_PER_MINUTE:
            return max(1, math.ceil(TEST_WINDOW_SECONDS - (now - _test_sends[0])))
        _test_sends.append(now)
        return None


class SmtpSettingsPublic(BaseModel):
    host: str
    port: int
    security: Literal["starttls", "tls"]
    username: str | None
    # The password itself is never returned.
    password_set: bool
    from_address: str
    from_name: str | None
    tls_ca_pem: str | None
    enabled: bool
    updated_at: datetime

    @classmethod
    def from_row(cls, row: SmtpSettings) -> "SmtpSettingsPublic":
        return cls(
            host=row.host,
            port=row.port,
            security=row.security,
            username=row.username,
            password_set=row.password_encrypted is not None,
            from_address=row.from_address,
            from_name=row.from_name,
            tls_ca_pem=row.tls_ca_pem,
            enabled=row.enabled,
            updated_at=row.updated_at,
        )


class SmtpSettingsUpdate(BaseModel):
    """PUT /smtp-settings: the whole configuration, except the password,
    which is left as it is when omitted (null) and removed when "". A
    username left empty means a relay without authentication, and removes
    any saved password."""

    host: str = Field(max_length=MAX_HOST)
    port: int = 587
    security: Literal["starttls", "tls"] = "starttls"
    username: str | None = Field(default=None, max_length=MAX_USERNAME)
    password: str | None = Field(default=None, max_length=MAX_PASSWORD)
    from_address: str = Field(max_length=profile.MAX_EMAIL)
    from_name: str | None = Field(default="PerchTail", max_length=profile.MAX_DISPLAY_NAME)
    tls_ca_pem: str | None = Field(default=None, max_length=MAX_CA_PEM)
    enabled: bool = False


class SmtpTestRequest(BaseModel):
    # Defaults to the signed-in user's own email.
    recipient: str | None = Field(default=None, max_length=profile.MAX_EMAIL)


class SmtpTestResult(BaseModel):
    ok: bool
    detail: str


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def _validate_host(raw: str) -> str:
    host = raw.strip().lower()
    if not host:
        raise _invalid("Enter the SMTP server's host name or address")
    try:
        ip_address(host)
    except ValueError:
        if not _HOSTNAME.match(host):
            raise _invalid("The SMTP server must be a host name or an IP address") from None
        host = host.rstrip(".")
    try:
        resolve_validated_smtp_ips(host)
    except UnsafeSmtpHostError as exc:
        raise _invalid(str(exc)) from exc
    return host


def _validate_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise _invalid("The port must be between 1 and 65535")
    return port


def _validate_username(raw: str | None) -> str | None:
    if raw is None:
        return None
    username = raw.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in username):
        raise _invalid("The username can't contain control characters")
    return username or None


def _validate_from_address(raw: str) -> str:
    try:
        address = profile.normalize_email(raw)
    except profile.ProfileError as exc:
        raise _invalid("The sender must be a valid email address") from exc
    if address is None:
        raise _invalid("Enter the sender address")
    return address


def _validate_from_name(raw: str | None) -> str | None:
    try:
        return profile.normalize_display_name(raw)
    except profile.ProfileError as exc:
        raise _invalid(f"Sender name: {exc}") from exc


def _validate_ca_pem(raw: str | None) -> str | None:
    """None/"" removes it; anything else must be PEM CA certificates that
    TLS can load."""
    if not raw or not raw.strip():
        return None
    pem = raw.strip() + "\n"
    try:
        certificates = x509.load_pem_x509_certificates(pem.encode())
    except ValueError as exc:
        raise _invalid("The CA certificate isn't valid PEM") from exc
    if not certificates:
        raise _invalid("The CA certificate contains no certificate")
    try:
        ssl.create_default_context().load_verify_locations(cadata=pem)
    except ssl.SSLError as exc:
        raise _invalid("The CA certificate can't be used for TLS") from exc
    return pem


def _snapshot(row: SmtpSettings | None) -> dict:
    return {name: getattr(row, name) if row else None for name in _AUDITED_FIELDS}


def _audit(session: Session, user: User, action: str, metadata: dict | None) -> None:
    # One configuration per installation, so its events carry no id (see
    # app.audit_labels.UNNAMED_TARGET_TYPES). Never the password.
    record_audit_event(
        session,
        user_id=user.id,
        action=action,
        target_type="smtp_settings",
        metadata=metadata,
    )


@router.get("", response_model=SmtpSettingsPublic | None)
def read_settings(
    user: User = Depends(require_super_admin), session: Session = Depends(get_session)
):
    """The SMTP configuration, or null when there's none."""
    row = get_smtp_settings(session)
    return SmtpSettingsPublic.from_row(row) if row else None


@router.put("", response_model=SmtpSettingsPublic)
def put_settings(
    payload: SmtpSettingsUpdate,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    host = _validate_host(payload.host)
    port = _validate_port(payload.port)
    username = _validate_username(payload.username)
    from_address = _validate_from_address(payload.from_address)
    from_name = _validate_from_name(payload.from_name)
    ca_pem = _validate_ca_pem(payload.tls_ca_pem)
    if username is None and payload.password:
        raise _invalid("A password needs a username")
    # smtplib can only send ASCII credentials.
    if (username and not username.isascii()) or (
        payload.password and not payload.password.isascii()
    ):
        raise _invalid(NON_ASCII_CREDENTIALS)

    row = get_smtp_settings(session)
    created = row is None
    before = _snapshot(row)
    had_password = row is not None and row.password_encrypted is not None
    old_ca = row.tls_ca_pem if row else None
    if row is None:
        row = SmtpSettings(host=host, port=port, from_address=from_address, updated_at=utcnow())

    row.host = host
    row.port = port
    row.security = payload.security
    row.username = username
    row.from_address = from_address
    row.from_name = from_name
    row.tls_ca_pem = ca_pem
    row.enabled = payload.enabled
    # No username: nothing would use a saved password, so it isn't kept.
    remove_secret = username is None or (payload.password is not None and not payload.password)
    secret_change = None
    if remove_secret:
        if had_password:
            row.password_encrypted = None
            secret_change = _REMOVED
    elif payload.password is not None:
        row.password_encrypted = encrypt_secret(payload.password)
        secret_change = _CHANGED
    row.updated_at = utcnow()
    session.add(row)
    session.flush()

    after = _snapshot(row)
    changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    if secret_change:
        changes["password"] = secret_change
    if ca_pem != old_ca:
        changes["tls_ca_pem"] = "replaced" if ca_pem else "removed"
    _audit(
        session,
        user,
        "smtp_settings.create" if created else "smtp_settings.update",
        {"changes": changes} if changes else None,
    )
    session.commit()
    session.refresh(row)
    return SmtpSettingsPublic.from_row(row)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_settings(
    user: User = Depends(require_super_admin), session: Session = Depends(get_session)
):
    row = get_smtp_settings(session)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email isn't configured")
    before = _snapshot(row)
    session.delete(row)
    _audit(session, user, "smtp_settings.delete", {"before": before})
    session.commit()


@router.post("/test", response_model=SmtpTestResult)
def send_test(
    payload: SmtpTestRequest | None = None,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Sends a fixed test message, to the given recipient or to the caller's
    own email. Limited per installation, and audited with the recipient and
    the outcome."""
    raw = payload.recipient if payload and payload.recipient else user.email
    if not raw:
        raise _invalid("Enter a recipient: your account has no email address")
    try:
        recipient = profile.normalize_email(raw)
    except profile.ProfileError as exc:
        raise _invalid("The recipient must be a valid email address") from exc
    if recipient is None:
        raise _invalid("Enter a recipient: your account has no email address")

    retry_after = _take_test_slot()
    if retry_after is not None:
        _audit(
            session,
            user,
            "smtp_settings.test",
            {"recipient": recipient, "result": "rate_limited"},
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many test messages; wait a minute and try again",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        send_email(session, recipient, TEST_MESSAGE, TEST_MESSAGE)
    except EmailNotConfiguredError as exc:
        result = SmtpTestResult(ok=False, detail=str(exc))
        outcome = {"result": "not_configured", "error": str(exc)}
    except EmailDeliveryError as exc:
        result = SmtpTestResult(ok=False, detail=str(exc))
        outcome = {"result": "failed", "error": str(exc)}
    else:
        result = SmtpTestResult(ok=True, detail=f"Test message sent to {recipient}")
        outcome = {"result": "sent"}

    _audit(session, user, "smtp_settings.test", {"recipient": recipient, **outcome})
    session.commit()
    return result
