from datetime import datetime

import pytest
from app.api.auth import get_current_active_user
from app.api.customers import router as customers_router
from app.api.folders import router as folders_router
from app.api.roles import router as roles_router
from app.api.sources import router as sources_router
from app.auth.models import Role, RoleGrant, ScopeType, SSOGroupRoleMapping, User
from app.db import get_session
from app.models import (
    Alert,
    Customer,
    Folder,
    Protocol,
    SearchIndexState,
    SeverityLevel,
    SeverityPattern,
    Source,
)
from app.search_index import _insert_fts_rows, search
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


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
    for router in (sources_router, folders_router, customers_router, roles_router):
        app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _customer(session, name="c") -> Customer:
    customer = Customer(name=name)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


def _source(session, customer) -> Source:
    source = Source(
        name="s", customer_id=customer.id, protocol=Protocol.ssh, host="h", base_path="/"
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _grant(session, scope_type, scope_id) -> None:
    role = Role(name=f"r-{scope_type}-{scope_id}")
    session.add(role)
    session.commit()
    session.add(
        RoleGrant(role_id=role.id, scope_type=scope_type, scope_id=scope_id, capabilities=["view"])
    )
    session.commit()


def test_deleted_source_id_is_never_reused(session, client):
    customer = _customer(session)
    old = _source(session, customer)
    assert client.delete(f"/sources/{old.id}").status_code == 204
    new = _source(session, customer)
    assert new.id != old.id


def test_deleting_a_source_removes_everything_pointing_at_it(session, client):
    customer = _customer(session)
    source = _source(session, customer)
    _grant(session, ScopeType.source, source.id)
    session.add(
        SearchIndexState(source_id=source.id, file_path="a.log", size=1, indexed_at=datetime.now())
    )
    session.add(
        Alert(
            user_id=1,
            name="a",
            query="q",
            source_id=source.id,
            webhook_url="https://example.com",
            created_at=datetime.now(),
        )
    )
    session.add(SeverityPattern(source_id=source.id, level=SeverityLevel.error, pattern="ERROR"))
    _insert_fts_rows(session, source.id, "a.log", "secret token here")
    session.commit()

    assert client.delete(f"/sources/{source.id}").status_code == 204

    for model, column in (
        (RoleGrant, RoleGrant.scope_id),
        (SearchIndexState, SearchIndexState.source_id),
        (Alert, Alert.source_id),
        (SeverityPattern, SeverityPattern.source_id),
    ):
        assert session.exec(select(model).where(column == source.id)).all() == []
    assert search(session, "secret", None) == []


def test_deleting_a_folder_or_customer_removes_its_grants(session, client):
    customer = _customer(session)
    folder = Folder(name="f", customer_id=customer.id)
    session.add(folder)
    session.commit()
    _grant(session, ScopeType.folder, folder.id)
    _grant(session, ScopeType.customer, customer.id)

    assert client.delete(f"/folders/{folder.id}").status_code == 204
    assert client.delete(f"/customers/{customer.id}").status_code == 204
    assert session.exec(select(RoleGrant)).all() == []


def test_role_mapped_from_sso_cannot_be_deleted(session, client):
    role = Role(name="mapped")
    session.add(role)
    session.commit()
    session.add(SSOGroupRoleMapping(order=0, group_name="g", role_id=role.id))
    session.commit()
    assert client.delete(f"/roles/{role.id}").status_code == 409
