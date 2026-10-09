import pytest
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.api.sources import router as sources_router
from app.auth.models import Role, User
from app.collectors import local as local_collector
from app.config import get_settings
from app.db import get_session
from app.models import Customer, PatternKind, Protocol, Rule, RuleType, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def allowed_root(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "local_source_roots", str(root))
    return root


@pytest.fixture()
def client(session):
    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)

    app = FastAPI()
    app.include_router(sources_router)
    app.include_router(archive_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _customer(session) -> Customer:
    customer = Customer(name="c")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


def _local_payload(customer, base_path):
    return {
        "name": "local",
        "customer_id": customer.id,
        "protocol": "local",
        "host": "localhost",
        "base_path": str(base_path),
    }


def test_local_source_outside_allowed_roots_is_rejected(session, client, allowed_root, tmp_path):
    customer = _customer(session)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert client.post("/sources", json=_local_payload(customer, outside)).status_code == 400
    assert client.post("/sources", json=_local_payload(customer, "/")).status_code == 400


def test_local_source_inside_allowed_root_is_accepted(session, client, allowed_root):
    customer = _customer(session)
    (allowed_root / "app").mkdir()
    response = client.post("/sources", json=_local_payload(customer, allowed_root / "app"))
    assert response.status_code == 201


def test_no_local_sources_at_all_when_roots_unset(session, client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_source_roots", "")
    customer = _customer(session)
    assert client.post("/sources", json=_local_payload(customer, tmp_path)).status_code == 400


def test_base_path_update_is_validated_too(session, client, allowed_root, tmp_path):
    customer = _customer(session)
    created = client.post("/sources", json=_local_payload(customer, allowed_root)).json()
    response = client.patch(f"/sources/{created['id']}", json={"base_path": str(tmp_path)})
    assert response.status_code == 400


def test_preexisting_disallowed_local_source_cannot_be_read(
    session, client, allowed_root, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.log").write_text("secret")
    source = Source(name="old", protocol=Protocol.local, host="h", base_path=str(outside))
    session.add(source)
    session.commit()
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

    assert client.get(f"/sources/{source.id}/browse").status_code == 403
    assert client.get(f"/sources/{source.id}/open", params={"path": "app.log"}).status_code == 403
    with pytest.raises(local_collector.LocalPathNotAllowedError):
        local_collector.resolve_path(source, "app.log")


def test_system_source_is_exempt_from_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_source_roots", "")
    source = Source(
        name="sys", protocol=Protocol.local, host="h", base_path=str(tmp_path), is_system=True
    )
    assert local_collector.resolve_path(source, "a.log") == tmp_path.resolve() / "a.log"


def test_symlink_out_of_base_path_is_not_followed(allowed_root, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (allowed_root / "link.log").symlink_to(secret)
    (allowed_root / "real.log").write_text("ok")
    source = Source(name="s", protocol=Protocol.local, host="h", base_path=str(allowed_root))
    rules = [
        Rule(
            source_id=1,
            order=0,
            type=RuleType.include,
            pattern="**/*",
            pattern_kind=PatternKind.glob,
        )
    ]

    names = [e.name for e in local_collector.list_directory(source, rules)]
    assert names == ["real.log"]
    with pytest.raises(local_collector.LocalPathNotAllowedError):
        local_collector.resolve_path(source, "link.log")
