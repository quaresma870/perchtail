"""Property-based API tests (#376): Schemathesis generates requests from the
app's own OpenAPI schema and sends them in-process, signed in as a super
admin, against a temporary database. The one check is that no response is a
server error.

Deterministic and bounded: a fixed seed, at most SCHEMATHESIS_MAX_EXAMPLES
examples per operation (25 by default; raise it by hand for a longer run).

No request leaves the machine: operations that talk to sources, agents,
webhooks, the SSO provider or GitHub are excluded, and any connection to an
address other than 127.0.0.1 fails the test while it runs."""

import os
import socket

import pytest
import schemathesis
from app.api.auth import get_current_user
from app.auth.models import Role, User
from app.db import get_session
from app.main import app
from app.name_index_store import ensure_name_index_schema
from fastapi import Depends
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

MAX_EXAMPLES = int(os.environ.get("SCHEMATHESIS_MAX_EXAMPLES", "25"))

# (method, path) of the operations left out, and why.
EXCLUDED_OPERATIONS = {
    # Read from sources (and their archives) through the connectors.
    ("GET", "/sources/{source_id}/browse"),
    ("GET", "/sources/{source_id}/open"),
    ("GET", "/sources/{source_id}/follow"),
    ("GET", "/sources/{source_id}/download"),
    ("GET", "/sources/{source_id}/download-zip"),
    # Connection test, "index now" and folder picker reach the source's host.
    ("POST", "/sources/{source_id}/check"),
    ("POST", "/sources/{source_id}/name-index"),
    ("POST", "/sources/pick-folder"),
    # Tests the new credential against each source first.
    ("POST", "/sources/credentials"),
    # Sends a test notification to the alert's webhook.
    ("POST", "/alerts/{alert_id}/test"),
    # Asks GitHub for the latest release.
    ("POST", "/system/update/check"),
    # Talk to the SSO provider.
    ("POST", "/sso/{provider_id}/test"),
    ("GET", "/auth/sso/login"),
    ("GET", "/auth/sso/callback"),
    # Would end the test user's session.
    ("POST", "/auth/logout"),
}

LOCAL_ADDRESSES = {"127.0.0.1"}


class _NoLifespan:
    """The app without its lifespan: Schemathesis would otherwise start it,
    and with it the real database, the scheduler and its jobs."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # Refusing the scope before reading a message means "no lifespan".
            raise RuntimeError("lifespan disabled in tests")
        await self.app(scope, receive, send)


def _excluded(ctx) -> bool:
    return (ctx.operation.method.upper(), ctx.operation.path) in EXCLUDED_OPERATIONS


schema = schemathesis.openapi.from_asgi("/openapi.json", _NoLifespan(app)).exclude(_excluded)


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(scope="module", autouse=True)
def _block_outside_network():
    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            if host not in LOCAL_ADDRESSES:
                raise NetworkBlockedError(
                    f"test_api_properties: an operation tried to connect to {address!r}; "
                    "add it to EXCLUDED_OPERATIONS"
                )
        return real_connect(sock, address)

    mp = pytest.MonkeyPatch()
    mp.setattr(socket.socket, "connect", guarded_connect)
    yield
    mp.undo()


@pytest.fixture(scope="module", autouse=True)
def _temporary_database_and_super_admin():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    ensure_name_index_schema(engine)
    with Session(engine) as session:
        role = Role(name="Property tests super admin", is_super_admin=True)
        session.add(role)
        session.commit()
        session.refresh(role)
        user = User(username="property-tests@example.com", role_id=role.id)
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id

    def _session():
        with Session(engine) as session:
            yield session

    def _user(session: Session = Depends(get_session)) -> User:
        return session.get(User, user_id)

    mp = pytest.MonkeyPatch()
    # The few background paths that open their own session use app.db.engine.
    mp.setattr("app.db.engine", engine)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = _user
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_user, None)
    mp.undo()
    engine.dispose()


def test_excluded_operations_still_exist():
    operations = {
        (method.upper(), path) for path, item in app.openapi()["paths"].items() for method in item
    }
    missing = sorted(EXCLUDED_OPERATIONS - operations)
    assert not missing, (
        "EXCLUDED_OPERATIONS lists operations the app no longer has; fix or remove them: "
        + ", ".join(f"{method} {path}" for method, path in missing)
    )


@schema.parametrize()
@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=list(HealthCheck),
)
def test_no_server_errors(case):
    case.call_and_validate(checks=[not_a_server_error])
