import csv
import io
import uuid

import pytest
from app.api.access_report import router as access_report_router
from app.api.auth import get_current_active_user
from app.audit_integrity import verify_chain
from app.auth.models import AuditLog, Capability, GlobalCapability, Role, ScopeType, User
from app.auth.rbac import create_role_grant
from app.crypto import audit_chain_key
from app.db import get_session
from app.models import Customer, Protocol, Source
from app.timeutils import utcnow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


def _make_user(session, *, username=None, is_super_admin=False, global_capabilities=None) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=is_super_admin,
        global_capabilities=global_capabilities or [],
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(
        username=username or f"user-{uuid.uuid4().hex}", role_id=role.id, last_login_at=utcnow()
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(access_report_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


@pytest.fixture()
def source(session):
    customer = Customer(name="Acme")
    session.add(customer)
    session.commit()
    source = Source(
        name="app01",
        customer_id=customer.id,
        protocol=Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _report_entries(session):
    return session.exec(select(AuditLog).where(AuditLog.action == "access_report.generate")).all()


def test_super_admin_gets_the_json_report_and_it_is_audited(session, client_for, source):
    admin = _make_user(session, username="root", is_super_admin=True)

    response = client_for(admin).get("/access-report")

    assert response.status_code == 200
    body = response.json()
    assert (body["scope"], body["view"], body["parameters"]) == (
        "full",
        "users",
        {"inactive_days": 90},
    )
    assert body["generated_by"] == {"id": admin.id, "username": "root"}
    assert body["users"][0]["access"][0]["source_name"] == "app01"

    (entry,) = _report_entries(session)
    assert entry.user_id == admin.id
    assert entry.target_type == "access_report"
    assert entry.event_metadata == {
        "view": "users",
        "format": "json",
        "scope": "full",
        "inactive_days": 90,
        "users": 1,
        "rows": 1,
    }
    assert entry.row_hash is not None
    assert verify_chain(audit_chain_key(), session).ok


def test_view_access_report_is_enough_and_gives_a_delegated_report(session, client_for, source):
    reviewer = _make_user(session, global_capabilities=[GlobalCapability.view_access_report])
    create_role_grant(
        session,
        actor_user_id=None,
        role_id=reviewer.role_id,
        scope_type=ScopeType.source,
        scope_id=source.id,
        capabilities=[Capability.view],
    )

    response = client_for(reviewer).get("/access-report", params={"view": "sources"})

    assert response.status_code == 200
    body = response.json()
    assert (body["scope"], body["view"]) == ("delegated", "sources")
    assert [s["source_id"] for s in body["sources"]] == [source.id]
    assert _report_entries(session)[0].event_metadata["scope"] == "delegated"


@pytest.mark.parametrize(
    "global_capabilities",
    [[], [GlobalCapability.manage_users], [GlobalCapability.manage_roles]],
)
def test_without_view_access_report_it_is_refused_with_no_data(
    session, client_for, source, global_capabilities
):
    user = _make_user(session, global_capabilities=global_capabilities)

    for params in ({}, {"format": "csv"}, {"inactive_days": "0"}):
        response = client_for(user).get("/access-report", params=params)
        assert response.status_code == 403
        assert response.json() == {"detail": "Not permitted"}
    assert _report_entries(session) == []


@pytest.mark.parametrize(
    "params",
    [
        {"inactive_days": "0"},
        {"inactive_days": "3651"},
        {"inactive_days": "soon"},
        {"view": "roles"},
        {"format": "xlsx"},
    ],
)
def test_invalid_parameters_are_422_and_generate_nothing(session, client_for, source, params):
    admin = _make_user(session, is_super_admin=True)

    assert client_for(admin).get("/access-report", params=params).status_code == 422
    assert _report_entries(session) == []


def test_inactive_days_bounds_are_accepted(session, client_for, source):
    admin = _make_user(session, is_super_admin=True)
    for days in ("1", "3650"):
        response = client_for(admin).get("/access-report", params={"inactive_days": days})
        assert response.status_code == 200
        assert response.json()["parameters"] == {"inactive_days": int(days)}


def test_csv_download(session, client_for, source):
    admin = _make_user(session, username="root", is_super_admin=True)
    _make_user(session, username="=cmd|' /C calc'!A0", is_super_admin=True)

    response = client_for(admin).get(
        "/access-report", params={"format": "csv", "view": "users", "inactive_days": "30"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    today = utcnow().strftime("%Y-%m-%d")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="perchtail-access-report-users-{today}.csv"'
    )
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][:3] == ["generated_at", "generated_by", "username"]
    assert {r[2] for r in rows[1:]} == {"root", "'=cmd|' /C calc'!A0"}
    assert all(r[0].endswith("Z") and r[1] == "root" for r in rows[1:])

    (entry,) = _report_entries(session)
    assert entry.event_metadata["format"] == "csv"
    assert entry.event_metadata["inactive_days"] == 30
    assert (entry.event_metadata["users"], entry.event_metadata["rows"]) == (2, 2)
