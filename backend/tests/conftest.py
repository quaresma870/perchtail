import pytest
from app import models  # noqa: F401  registers tables on SQLModel.metadata
from app.auth import models as auth_models  # noqa: F401  registers tables on SQLModel.metadata
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

assert models and auth_models


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
    with Session(engine) as session:
        yield session
