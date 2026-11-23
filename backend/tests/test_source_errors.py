"""#170: predictable source-side failures get their own status and an
actionable message instead of a bare 500, and view-only users never see
raw library error text."""

import errno
import ssl
import uuid

import pytest
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.api.errors import install as install_error_handlers
from app.api.sources import router as sources_router
from app.auth.models import Capability, Role, RoleGrant, ScopeType, User
from app.collectors import errors
from app.collectors.base import (
    SourceAuthError,
    SourceHostKeyError,
    SourceLockedError,
    SourceNotFoundError,
    SourcePermissionError,
    SourceUnreachableError,
)
from app.collectors.registry import CONNECTORS
from app.db import get_session
from app.models import Customer, PatternKind, Protocol, Rule, RuleType, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeSMBOSError(OSError):
    def __init__(self, ntstatus):
        super().__init__("smb failure")
        self.ntstatus = ntstatus


@pytest.mark.parametrize(
    "exc, expected",
    [
        (PermissionError(errno.EACCES, "Permission denied"), SourcePermissionError),
        (FileNotFoundError(errno.ENOENT, "No such file"), SourceNotFoundError),
        (OSError(errno.ENOENT, "No such file"), SourceNotFoundError),
        (ConnectionRefusedError("refused"), SourceUnreachableError),
        (TimeoutError("timed out"), SourceUnreachableError),
    ],
)
def test_common_os_errors_are_mapped(exc, expected):
    assert isinstance(errors.translate_ssh(exc), expected)


def test_ssh_specific_errors_are_mapped():
    import paramiko

    assert isinstance(
        errors.translate_ssh(paramiko.AuthenticationException("bad")), SourceAuthError
    )
    bad = paramiko.BadHostKeyException("h", _DummyKey("a"), _DummyKey("b"))
    assert isinstance(errors.translate_ssh(bad), SourceHostKeyError)


class _DummyKey:
    def __init__(self, tag):
        self.tag = tag

    def get_base64(self):
        return self.tag


@pytest.mark.parametrize(
    "ntstatus, expected",
    [
        (0xC0000034, SourceNotFoundError),
        (0xC00000CC, SourceNotFoundError),
        (0xC0000022, SourcePermissionError),
        (0xC0000043, SourceLockedError),
        (0xC000006D, SourceAuthError),
    ],
)
def test_smb_ntstatus_codes_are_mapped(ntstatus, expected):
    assert isinstance(errors.translate_smb(_FakeSMBOSError(ntstatus)), expected)


def test_smb_bad_share_name_message_explains_base_path():
    mapped = errors.translate_smb(_FakeSMBOSError(0xC00000CC))
    assert "share name" in mapped.public_message


@pytest.mark.parametrize(
    "stderr, expected",
    [
        ("Cannot find path 'C:\\x' because it does not exist.", SourceNotFoundError),
        ("Access to the path 'C:\\x' is denied.", SourcePermissionError),
        (
            "The process cannot access the file because it is being used by another process.",
            SourceLockedError,
        ),
    ],
)
def test_winrm_powershell_errors_are_mapped(stderr, expected):
    assert isinstance(errors.translate_winrm(RuntimeError(stderr)), expected)


def test_unknown_errors_are_left_alone():
    assert errors.translate_ssh(ValueError("something else")) is None


# --- through the API -------------------------------------------------------


def _user(session, *, super_admin=False, customer=None, capabilities=(Capability.view,)):
    role = Role(name=f"r-{uuid.uuid4().hex}", is_super_admin=super_admin)
    session.add(role)
    session.commit()
    if customer is not None:
        session.add(
            RoleGrant(
                role_id=role.id,
                scope_type=ScopeType.customer,
                scope_id=customer.id,
                capabilities=list(capabilities),
            )
        )
        session.commit()
    user = User(username=f"u-{uuid.uuid4().hex}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _client(session, user):
    app = FastAPI()
    app.include_router(archive_router)
    app.include_router(sources_router)
    install_error_handlers(app)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _source(session):
    customer = Customer(name=f"c-{uuid.uuid4().hex}")
    session.add(customer)
    session.commit()
    source = Source(
        name="app01",
        customer_id=customer.id,
        protocol=Protocol.ssh,
        host="h",
        base_path="/var/log/app",
        credential_ref="x",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**/*",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    return customer, source


def test_permission_error_on_open_is_a_403_with_a_safe_message(session, monkeypatch):
    customer, source = _source(session)
    client = _client(session, _user(session, super_admin=True))

    def boom(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied: /var/log/app/ilegivel.log")

    monkeypatch.setattr(CONNECTORS[Protocol.ssh]._module, "fetch_file", boom)

    response = client.get(f"/sources/{source.id}/open", params={"path": "ilegivel.log"})

    assert response.status_code == 403
    assert "/var/log/app" not in response.json()["detail"]


def test_check_needs_manage_source_and_never_returns_the_raw_error(session, monkeypatch):
    customer, source = _source(session)

    def boom(*args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "No such file: \\\\internal-host\\secret$")

    monkeypatch.setattr(CONNECTORS[Protocol.ssh]._module, "list_directory", boom)

    viewer = _client(session, _user(session, customer=customer))
    manager = _client(
        session,
        _user(session, customer=customer, capabilities=(Capability.view, Capability.manage_source)),
    )
    admin = _client(session, _user(session, super_admin=True))

    assert viewer.post(f"/sources/{source.id}/check").status_code == 403
    for client in (manager, admin):
        answer = client.post(f"/sources/{source.id}/check").json()
        assert answer["ok"] is False
        assert "internal-host" not in answer["detail"]
        assert "/var/log/app" in answer["detail"]
        assert "ChrootDirectory" in answer["detail"]


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        TimeoutError("timed out"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
        EOFError("closed while reading the banner"),
    ],
)
def test_check_and_picker_say_the_same_for_every_failed_connection(session, monkeypatch, failure):
    """A closed port, a refused connection, a timeout or an error the
    connector doesn't map all get the same neutral answer, so neither
    endpoint can be used to probe which ports answer."""
    customer, source = _source(session)

    def boom(*args, **kwargs):
        raise failure

    monkeypatch.setattr(CONNECTORS[Protocol.ssh]._module, "list_directory", boom)
    admin = _client(session, _user(session, super_admin=True))

    check = admin.post(f"/sources/{source.id}/check").json()
    assert check == {"ok": False, "detail": "Can't reach the source (base path: /var/log/app)"}

    picked = admin.post(
        "/sources/pick-folder",
        json={
            "protocol": "ssh",
            "host": "10.0.0.9",
            "port": 2222,
            "credential": {"username": "u", "password": "p"},
            "path": "/",
        },
    )
    assert (picked.status_code, picked.json()) == (502, {"detail": "Can't reach the source"})


@pytest.mark.parametrize(
    "message, expected_name",
    [
        ("openat secret: path escapes from parent", "PathOutsideBaseError"),
        ('unsafe path: "../etc"', "PathOutsideBaseError"),
        ("open x.log: no such file or directory", "SourceNotFoundError"),
        ("open x.log: permission denied", "SourcePermissionError"),
        ("not a directory", "SourceNotFoundError"),
        # A Windows agent's wording (#272, found in the 0.6.0-rc.1 lab).
        (
            "openat app/gone.log: The system cannot find the file specified.",
            "SourceNotFoundError",
        ),
        ("openat gone/x.log: The system cannot find the path specified.", "SourceNotFoundError"),
        ("readdirent app.log: The directory name is invalid.", "SourceNotFoundError"),
        ("open secret.log: Access is denied.", "SourcePermissionError"),
    ],
)
def test_agent_refusals_map_like_the_other_connectors(message, expected_name):
    """F-L: a path the agent refuses (a link out of its base) was a 502;
    now it's a 404, as over SSH."""
    from app.agent_registry import AgentCommandError

    assert type(errors.translate_agent(AgentCommandError(message))).__name__ == expected_name


@pytest.mark.parametrize(
    "text, fragment",
    [
        (
            "certificate verify failed: Hostname mismatch, certificate is not valid for 'x'.",
            "different name",
        ),
        ("certificate verify failed: certificate has expired", "expired"),
        ("certificate verify failed: unable to get local issuer certificate", "issuer is unknown"),
    ],
)
def test_winrm_tls_errors_say_which_check_failed(text, fragment):
    """F-N: a certificate for another name got the same "isn't trusted" as
    one from an unknown CA, though the fix is different."""
    import requests

    mapped = errors.translate_winrm(requests.exceptions.SSLError(text))
    assert fragment in mapped.public_message


def _cert_error(code: int, message: str) -> ssl.SSLCertVerificationError:
    exc = ssl.SSLCertVerificationError(1, f"[SSL: CERTIFICATE_VERIFY_FAILED] {message}")
    exc.verify_code = code
    exc.verify_message = message
    return exc


@pytest.mark.parametrize(
    "code, message, fragment",
    [
        (64, "IP address mismatch, certificate is not valid for '10.0.0.5'.", "different name"),
        (62, "Hostname mismatch, certificate is not valid for 'x'.", "different name"),
        (10, "certificate has expired", "expired"),
        (20, "unable to get local issuer certificate", "issuer is unknown"),
        (19, "self-signed certificate in certificate chain", "issuer is unknown"),
    ],
)
def test_winrm_tls_errors_are_decided_by_the_verify_code(code, message, fragment):
    """#203: with an IP host, OpenSSL says "IP address mismatch", which the
    text match missed. The verify code is checked through the wrapping that
    requests/urllib3 add."""
    import requests
    from urllib3.exceptions import MaxRetryError
    from urllib3.exceptions import SSLError as Urllib3SSLError

    inner = Urllib3SSLError(_cert_error(code, message))
    wrapped = requests.exceptions.SSLError(MaxRetryError(None, "/wsman", reason=inner))
    assert fragment in errors.translate_winrm(wrapped).public_message


@pytest.mark.parametrize("protocol", [Protocol.smb, Protocol.winrm, Protocol.ssh])
def test_a_source_without_a_credential_says_so(protocol):
    """#206: "Test connection" on an SMB source with no credential showed
    `'NoneType' object has no attribute …`."""
    from app.collectors.base import SourceNoCredentialError

    base_path = "D:\\Logs" if protocol == Protocol.winrm else "Logs"
    source = Source(name="x", protocol=protocol, host="h", base_path=base_path, credential_ref=None)
    with pytest.raises(SourceNoCredentialError) as raised:
        CONNECTORS[protocol].list_directory(source, [], "")
    assert "no credential" in raised.value.public_message


def test_a_read_limit_no_endpoint_caught_is_its_413_not_a_500():
    """F1 of the 0.7.0-rc.3 retest: a read over MAX_FETCH_MB or
    MAX_DECOMPRESSED_MB that an endpoint lets through gets the 413 the
    endpoints give (app/api/archive.py), never a 500."""
    from app.archives import DecompressionLimitError
    from app.collectors.base import FetchTooLargeError

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/fetch")
    def fetch():
        raise FetchTooLargeError("over", size=3 * 1024**2, limit=1024**2)

    @app.get("/decompress")
    def decompress():
        raise DecompressionLimitError("over")

    client = TestClient(app, raise_server_exceptions=False)

    fetch_answer = client.get("/fetch")
    decompress_answer = client.get("/decompress")

    assert fetch_answer.status_code == 413
    assert fetch_answer.json()["detail"] == {
        "message": "This file is 3.0 MB, over this server's 1 MB limit (MAX_FETCH_MB).",
        "error_code": "fetch_too_large",
        "size_bytes": 3 * 1024**2,
        "limit_bytes": 1024**2,
    }
    assert decompress_answer.status_code == 413
    assert decompress_answer.json()["detail"] == (
        "File is too large once decompressed (MAX_DECOMPRESSED_MB)"
    )
