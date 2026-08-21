import ipaddress
import socket

import pytest
from app import models  # noqa: F401  registers tables on SQLModel.metadata
from app.auth import models as auth_models  # noqa: F401  registers tables on SQLModel.metadata
from app.db import ensure_search_schema
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

assert models and auth_models

_FAKE_PUBLIC_IP = "93.184.216.34"


def _fake_getaddrinfo(host, *args, **kwargs):
    """Test-only DNS stub for app.webhook_safety's SSRF guard -- keeps
    webhook_url validation hermetic instead of depending on real DNS
    resolution being available (or resolving predictably) in CI. Literal
    IP addresses pass through untouched, so tests that exercise SSRF
    blocking against e.g. 127.0.0.1 or 169.254.169.254 still see the
    real address; any hostname resolves to a fixed public IP."""
    try:
        ipaddress.ip_address(host)
        resolved = host
    except ValueError:
        resolved = _FAKE_PUBLIC_IP
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, 0))]


@pytest.fixture(autouse=True)
def _stub_dns_for_webhook_safety(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


@pytest.fixture()
def session():
    # StaticPool: FastAPI's TestClient runs the app in a separate thread, and
    # the default SQLite in-memory pool is thread-affined — without this, the
    # endpoint sees a different, empty in-memory DB than the one this fixture
    # just created tables in ("no such table" errors that only show up when a
    # test goes through a TestClient rather than calling the session directly).
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    ensure_search_schema(engine)
    with Session(engine) as session:
        yield session
