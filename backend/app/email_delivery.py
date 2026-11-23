"""Sends email through the system SMTP server (#244), configured by a super
admin in Settings -> Email (app.models.SmtpSettings, app/api/smtp_settings.py).

Nothing is sent while there's no configuration or it's turned off. Every
send:

- resolves the server's host again and checks it against the SMTP address
  policy (app.webhook_safety.resolve_validated_smtp_ips), then connects to
  that already-checked IP -- never to the hostname, which could resolve
  somewhere else by then -- while TLS still verifies the certificate
  against the hostname (SNI and hostname check use the name, not the IP);
- uses TLS with certificate verification, always: implicit TLS, or STARTTLS
  called unconditionally, so a server that doesn't offer it fails the send
  instead of carrying on in plain text. ssl.create_default_context() (plus
  the configured CA, if any) because smtplib's own default context doesn't
  verify certificates;
- authenticates only after TLS is up;
- sends plain text only, marked Auto-Submitted.

Errors come back as short fixed messages (EmailDeliveryError): never the
password, the message or the server's own reply text.
"""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from cryptography.fernet import InvalidToken
from sqlmodel import Session, select

from app import profile
from app.crypto import decrypt_secret
from app.logging_config import get_logger
from app.models import SmtpSettings
from app.webhook_safety import UnsafeSmtpHostError, resolve_validated_smtp_ips

logger = get_logger(__name__)

SEND_TIMEOUT_SECONDS = 15
SECURITY_MODES = ("starttls", "tls")
NON_ASCII_CREDENTIALS = "The SMTP username and password must use ASCII characters only"


class EmailNotConfiguredError(Exception):
    """No SMTP server is configured, or it's turned off. Nothing was sent."""


class EmailDeliveryError(Exception):
    """The send failed; str() is a short message safe to show a super admin
    and to record (no secrets, no message content)."""


class _PinnedConnection:
    """Opens the TCP connection to `pinned_ip` instead of resolving the host
    again. smtplib keeps the hostname in self._host, which is what STARTTLS
    and SMTP_SSL pass as server_hostname, so the certificate is still
    checked against the name."""

    _pinned_ip: str

    def _get_socket(self, host, port, timeout):
        return super()._get_socket(self._pinned_ip, port, timeout)


class _PinnedSMTP(_PinnedConnection, smtplib.SMTP):
    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float):
        self._pinned_ip = pinned_ip
        super().__init__(host, port, timeout=timeout)


class _PinnedSMTPSSL(_PinnedConnection, smtplib.SMTP_SSL):
    def __init__(
        self, host: str, port: int, *, pinned_ip: str, timeout: float, context: ssl.SSLContext
    ):
        self._pinned_ip = pinned_ip
        super().__init__(host, port, timeout=timeout, context=context)


def get_smtp_settings(session: Session) -> SmtpSettings | None:
    return session.exec(select(SmtpSettings).order_by(SmtpSettings.id)).first()


def tls_context(ca_pem: str | None) -> ssl.SSLContext:
    """The system's trusted CAs and, when set, the configured ones too.
    Always verifies the certificate and the hostname."""
    context = ssl.create_default_context()
    if ca_pem:
        context.load_verify_locations(cadata=ca_pem)
    return context


def _build_message(config: SmtpSettings, to: str, subject: str, body_text: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((config.from_name or "", config.from_address))
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=config.from_address.rsplit("@", 1)[-1])
    # RFC 3834: tells autoresponders not to answer.
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body_text)
    return message


def _deliver(config: SmtpSettings, pinned_ip: str, password: str | None, message) -> None:
    context = tls_context(config.tls_ca_pem)
    if config.security == "tls":
        client = _PinnedSMTPSSL(
            config.host,
            config.port,
            pinned_ip=pinned_ip,
            timeout=SEND_TIMEOUT_SECONDS,
            context=context,
        )
    else:
        client = _PinnedSMTP(
            config.host, config.port, pinned_ip=pinned_ip, timeout=SEND_TIMEOUT_SECONDS
        )
    try:
        if config.security != "tls":
            try:
                client.starttls(context=context)
            except smtplib.SMTPNotSupportedError as exc:
                raise EmailDeliveryError("The SMTP server doesn't offer STARTTLS") from exc
            except smtplib.SMTPResponseException as exc:
                raise EmailDeliveryError(
                    f"The SMTP server refused STARTTLS (code {exc.smtp_code})"
                ) from exc
        if config.username:
            try:
                client.login(config.username, password or "")
            except smtplib.SMTPNotSupportedError as exc:
                raise EmailDeliveryError(
                    "The SMTP server doesn't offer authentication; clear the username "
                    "to send without it"
                ) from exc
            except smtplib.SMTPAuthenticationError as exc:
                raise EmailDeliveryError(
                    "The SMTP server refused the username or password"
                ) from exc
            except UnicodeError:
                # smtplib encodes credentials as ASCII. The original error
                # names a character of the password and its position, so it
                # isn't chained or logged.
                raise EmailDeliveryError(NON_ASCII_CREDENTIALS) from None
            except (smtplib.SMTPException, OSError):
                raise
            except Exception:
                # Anything else while authenticating is a failed send, never
                # an error escaping the caller's handling. Not chained, as
                # it could carry the credentials.
                raise EmailDeliveryError("Authentication with the SMTP server failed") from None
        client.send_message(message)
    finally:
        try:
            client.quit()
        except (smtplib.SMTPException, OSError):
            client.close()


def _describe(exc: BaseException) -> str:
    """A short message for an error smtplib or the socket raised: the kind
    of failure and the SMTP code, never the server's reply text."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "The SMTP server's TLS certificate couldn't be verified"
    if isinstance(exc, ssl.SSLError):
        return "The TLS connection to the SMTP server failed"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The SMTP server refused the recipient"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"The SMTP server refused the sender address (code {exc.smtp_code})"
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"The SMTP server refused the message (code {exc.smtp_code})"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "The SMTP server closed the connection"
    if isinstance(exc, smtplib.SMTPException):
        return "The SMTP conversation failed"
    if isinstance(exc, TimeoutError):
        return "Timed out talking to the SMTP server"
    return "Couldn't connect to the SMTP server"


def send_email(session: Session, to: str, subject: str, body_text: str) -> None:
    """Sends one plain-text email to `to`. Raises EmailNotConfiguredError
    when there's no SMTP server or it's turned off (no connection is made),
    and EmailDeliveryError for anything else that stops it."""
    config = get_smtp_settings(session)
    if config is None:
        raise EmailNotConfiguredError("Email isn't configured")
    if not config.enabled:
        raise EmailNotConfiguredError("Email is turned off")

    try:
        recipient = profile.normalize_email(to)
    except profile.ProfileError as exc:
        raise EmailDeliveryError("The recipient isn't a valid email address") from exc
    if recipient is None:
        raise EmailDeliveryError("The recipient isn't a valid email address")

    try:
        pinned_ip = resolve_validated_smtp_ips(config.host)[0]
    except UnsafeSmtpHostError as exc:
        logger.warning("email.failed", error="smtp_host_refused")
        raise EmailDeliveryError(str(exc)) from exc

    password = None
    if config.password_encrypted:
        try:
            password = decrypt_secret(config.password_encrypted)
        except InvalidToken as exc:
            logger.warning("email.failed", error="smtp_password_undecryptable")
            raise EmailDeliveryError(
                "The saved SMTP password can't be decrypted; enter it again"
            ) from exc
    message = _build_message(config, recipient, subject, body_text)
    try:
        _deliver(config, pinned_ip, password, message)
    except EmailDeliveryError as exc:
        logger.warning("email.failed", error=str(exc))
        raise
    except (smtplib.SMTPException, OSError) as exc:
        error = _describe(exc)
        logger.warning("email.failed", error=error, error_type=type(exc).__name__)
        raise EmailDeliveryError(error) from exc
    logger.info("email.sent")
