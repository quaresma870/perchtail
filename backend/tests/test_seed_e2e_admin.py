from app.auth.models import Role, User
from app.auth.providers.local import verify_password
from app.seed_e2e_admin import seed
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


def _throwaway_engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def test_seed_creates_a_working_super_admin(monkeypatch):
    engine = _throwaway_engine()
    monkeypatch.setattr("app.db.engine", engine)

    result = seed("e2e-admin", "e2e-test-password-123!")
    assert result == 0

    with Session(engine) as session:
        user = session.exec(select(User)).first()
        assert user is not None
        assert user.username == "e2e-admin"
        assert user.must_change_password is False
        assert verify_password(user, "e2e-test-password-123!")

        role = session.get(Role, user.role_id)
        assert role is not None
        assert role.is_super_admin is True


def test_seed_refuses_to_run_against_a_non_empty_database(monkeypatch):
    engine = _throwaway_engine()
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        role = Role(name="Existing", is_builtin=True, is_super_admin=False)
        session.add(role)
        session.flush()
        session.add(User(username="already-here", password_hash=None, role_id=role.id))
        session.commit()

    result = seed("e2e-admin", "e2e-test-password-123!")
    assert result == 1

    with Session(engine) as session:
        usernames = {u.username for u in session.exec(select(User)).all()}
    assert usernames == {"already-here"}
