"""#170: predictable source-side failures get their own status and an
actionable message instead of a bare 500, and view-only users never see
raw library error text."""

import errno
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


def _user(session, *, super_admin=False, customer=None):
    role = Role(name=f"r-{uuid.uuid4().hex}", is_super_admin=super_admin)
    session.add(role)
    session.commit()
    if customer is not None:
        session.add(
            RoleGrant(
                role_id=role.id,
                scope_type=ScopeType.customer,
                scope_id=customer.id,
                capabilities=[Capability.view],
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


def test_check_hides_raw_error_from_view_only_users(session, monkeypatch):
    customer, source = _source(session)

    def boom(*args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "No such file: \\\\internal-host\\secret$")

    monkeypatch.setattr(CONNECTORS[Protocol.ssh]._module, "list_directory", boom)

    viewer = _client(session, _user(session, customer=customer))
    admin = _client(session, _user(session, super_admin=True))

    as_viewer = viewer.post(f"/sources/{source.id}/check").json()
    as_admin = admin.post(f"/sources/{source.id}/check").json()

    assert as_viewer["ok"] is False
    assert "internal-host" not in as_viewer["detail"]
    assert "/var/log/app" in as_viewer["detail"]
    assert "ChrootDirectory" in as_viewer["detail"]
    assert "internal-host" in as_admin["detail"]
