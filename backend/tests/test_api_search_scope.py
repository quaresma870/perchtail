"""Narrowing a search to customers, folders, sources and paths (#163)."""

import pytest
from app.api.auth import get_current_active_user
from app.api.search import router as search_router
from app.auth.models import Capability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Customer, Folder, Protocol, Source
from app.search_index import _insert_fts_rows
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def world(session):
    acme, beta = Customer(name="Acme"), Customer(name="Beta")
    session.add_all([acme, beta])
    session.commit()
    prod = Folder(name="prod", customer_id=acme.id)
    session.add(prod)
    session.commit()
    web = Folder(name="web", customer_id=acme.id, parent_folder_id=prod.id)
    session.add(web)
    session.commit()

    def src(name, customer, folder=None):
        s = Source(
            name=name,
            customer_id=customer.id,
            folder_id=folder.id if folder else None,
            protocol=Protocol.ssh,
            host=name,
            base_path="/var/log",
            search_indexing_enabled=True,
        )
        session.add(s)
        session.commit()
        return s

    sources = {
        "acme-root": src("acme-root", acme),
        "acme-prod": src("acme-prod", acme, prod),
        "acme-web": src("acme-web", acme, web),
        "beta-src": src("beta-src", beta),
    }
    for s in sources.values():
        _insert_fts_rows(session, s.id, "iis/u_ex1.log", "needle in iis")
        _insert_fts_rows(session, s.id, "app/app_1.log", "needle in app")
    session.commit()
    return {"acme": acme, "beta": beta, "prod": prod, "web": web, **sources}


def _client(session, user):
    app = FastAPI()
    app.include_router(search_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _user(session, *, super_admin=False):
    role = Role(name=f"r{super_admin}", is_super_admin=super_admin)
    session.add(role)
    session.commit()
    user = User(username=f"u{super_admin}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _sources_hit(client, world, **params):
    response = client.get("/search", params={"q": "needle", **params})
    assert response.status_code == 200, response.text
    by_id = {s.id: name for name, s in world.items() if isinstance(s, Source)}
    return {by_id[h["source_id"]] for h in response.json()}


def test_scope_by_customer_folder_and_source(session, world):
    client = _client(session, _user(session, super_admin=True))
    assert _sources_hit(client, world) == {"acme-root", "acme-prod", "acme-web", "beta-src"}
    assert _sources_hit(client, world, customer_id=world["beta"].id) == {"beta-src"}
    # A folder covers its sub-folders too.
    assert _sources_hit(client, world, folder_id=world["prod"].id) == {"acme-prod", "acme-web"}
    assert _sources_hit(client, world, folder_id=world["web"].id) == {"acme-web"}
    assert _sources_hit(client, world, source_id=[world["beta-src"].id, world["acme-root"].id]) == {
        "beta-src",
        "acme-root",
    }
    # Several kinds of scope add up.
    assert _sources_hit(client, world, folder_id=world["web"].id, customer_id=world["beta"].id) == {
        "acme-web",
        "beta-src",
    }


def test_scope_never_widens_access(session, world):
    user = _user(session)
    session.add(
        RoleGrant(
            role_id=user.role_id,
            scope_type=ScopeType.folder,
            scope_id=world["web"].id,
            capabilities=[Capability.view],
        )
    )
    session.commit()
    client = _client(session, user)
    assert _sources_hit(client, world) == {"acme-web"}
    assert _sources_hit(client, world, customer_id=world["acme"].id) == {"acme-web"}
    assert _sources_hit(client, world, source_id=world["beta-src"].id) == set()


def test_path_filter_is_a_substring_or_a_glob(session, world):
    client = _client(session, _user(session, super_admin=True))
    response = client.get(
        "/search", params={"q": "needle", "path": "IIS/", "source_id": world["beta-src"].id}
    ).json()
    assert [h["file_path"] for h in response] == ["iis/u_ex1.log"]
    response = client.get(
        "/search", params={"q": "needle", "path": "app/*.log", "source_id": world["beta-src"].id}
    ).json()
    assert [h["file_path"] for h in response] == ["app/app_1.log"]
    # LIKE wildcards in a plain filter are literal.
    response = client.get(
        "/search", params={"q": "needle", "path": "app%", "source_id": world["beta-src"].id}
    ).json()
    assert response == []
    response = client.get(
        "/search", params={"q": "needle", "path": "app_1", "source_id": world["beta-src"].id}
    ).json()
    assert [h["file_path"] for h in response] == ["app/app_1.log"]


def test_scope_is_applied_before_the_limit(session, world):
    from app.search_index import search

    for i in range(80):
        _insert_fts_rows(session, world["beta-src"].id, f"noise/{i}.log", "needle noise")
    session.commit()
    hits = search(session, "needle", {world["beta-src"].id}, path_filter="iis")
    assert [h.file_path for h in hits] == ["iis/u_ex1.log"]
