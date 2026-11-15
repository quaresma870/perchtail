"""Customers and folders only within a role's grants (rbac.org_reach): the
lists show only what the role's grants reach, and anything outside answers
404, audited as access.denied. Also one grant per role and scope, a role's
grants readable only within the reader's authority, and the audit log API
turned off with its page."""

import uuid

import pytest
from app.api import roles as roles_api
from app.auth.models import (
    AuditLog,
    Capability,
    GlobalCapability,
    Role,
    RoleGrant,
    ScopeType,
    User,
)
from app.auth.rbac import customer_in_reach, folder_in_reach, org_reach
from app.auth.sessions import create_session
from app.db import get_session
from app.main import app
from app.models import Customer, Folder, Protocol, Source
from app.system_settings import set_bool
from fastapi.testclient import TestClient
from sqlmodel import select

VIEW = [Capability.view]
MANAGE = [Capability.view, Capability.manage_source]


def _user(session, *, super_admin=False, capabilities=(), grants=()) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=super_admin,
        global_capabilities=list(capabilities),
    )
    session.add(role)
    session.flush()
    for scope_type, scope_id, caps in grants:
        session.add(
            RoleGrant(role_id=role.id, scope_type=scope_type, scope_id=scope_id, capabilities=caps)
        )
    user = User(username=f"user-{uuid.uuid4().hex}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


class Org:
    """Customer A: A1 > A1a > A1a_i, and A2; source deep in A1a_i, source
    root at A's top. Customer B: B1 > B1a, source b in B1a. Customer C:
    nothing in it."""

    def __init__(self, session):
        self.a, self.b, self.c = Customer(name="A"), Customer(name="B"), Customer(name="C")
        session.add_all([self.a, self.b, self.c])
        session.flush()
        self.a1 = self._folder(session, self.a, "A1")
        self.a1a = self._folder(session, self.a, "A1a", self.a1)
        self.a1a_i = self._folder(session, self.a, "A1a_i", self.a1a)
        self.a2 = self._folder(session, self.a, "A2")
        self.b1 = self._folder(session, self.b, "B1")
        self.b1a = self._folder(session, self.b, "B1a", self.b1)
        self.deep = self._source(session, self.a, "deep", self.a1a_i)
        self.root = self._source(session, self.a, "root", None)
        self.source_b = self._source(session, self.b, "b", self.b1a)
        session.commit()

    @staticmethod
    def _folder(session, customer, name, parent=None) -> Folder:
        folder = Folder(
            name=name, customer_id=customer.id, parent_folder_id=parent.id if parent else None
        )
        session.add(folder)
        session.flush()
        return folder

    @staticmethod
    def _source(session, customer, name, folder) -> Source:
        source = Source(
            name=name,
            customer_id=customer.id,
            folder_id=folder.id if folder else None,
            protocol=Protocol.ssh,
            host=name,
            base_path="/var/log",
        )
        session.add(source)
        session.flush()
        return source

    def a_folders(self) -> set[int]:
        return {self.a1.id, self.a1a.id, self.a1a_i.id, self.a2.id}


@pytest.fixture()
def org(session) -> Org:
    return Org(session)


def _reach(session, user) -> tuple[set[int], set[int]]:
    reach = org_reach(session, user)
    assert reach is not None
    return set(reach.customer_ids), set(reach.folder_ids)


# --- org_reach ------------------------------------------------------------------


def test_a_super_admin_reaches_everything(session, org):
    user = _user(session, super_admin=True)
    assert org_reach(session, user) is None
    assert customer_in_reach(session, user, org.c.id)
    assert folder_in_reach(session, user, org.b1a.id)


def test_no_grants_reach_nothing(session, org):
    user = _user(session, capabilities=[GlobalCapability.create_source])
    assert _reach(session, user) == (set(), set())
    assert not customer_in_reach(session, user, org.a.id)
    assert not folder_in_reach(session, user, org.a1.id)


def test_a_customer_grant_reaches_the_customer_and_all_its_folders(session, org):
    user = _user(session, grants=[(ScopeType.customer, org.a.id, VIEW)])
    assert _reach(session, user) == ({org.a.id}, org.a_folders())


def test_a_deep_folder_grant_reaches_it_and_below_not_above(session, org):
    user = _user(session, grants=[(ScopeType.folder, org.a1a.id, VIEW)])
    assert _reach(session, user) == ({org.a.id}, {org.a1a.id, org.a1a_i.id})


def test_a_top_folder_grant_reaches_every_folder_below(session, org):
    user = _user(session, grants=[(ScopeType.folder, org.a1.id, VIEW)])
    assert _reach(session, user) == ({org.a.id}, {org.a1.id, org.a1a.id, org.a1a_i.id})


def test_a_source_grant_in_a_deep_folder_reaches_the_path_to_it(session, org):
    user = _user(session, grants=[(ScopeType.source, org.deep.id, VIEW)])
    # Its folder and those above it, for the path; not A2, nothing below.
    assert _reach(session, user) == ({org.a.id}, {org.a1a_i.id, org.a1a.id, org.a1.id})


def test_a_source_grant_at_the_customer_top_reaches_no_folder(session, org):
    user = _user(session, grants=[(ScopeType.source, org.root.id, VIEW)])
    assert _reach(session, user) == ({org.a.id}, set())


def test_grants_in_several_customers_add_up(session, org):
    user = _user(
        session,
        grants=[
            (ScopeType.folder, org.b1a.id, VIEW),
            (ScopeType.source, org.deep.id, VIEW),
            (ScopeType.folder, org.a1a.id, VIEW),
        ],
    )
    assert _reach(session, user) == (
        {org.a.id, org.b.id},
        {org.b1a.id, org.a1a_i.id, org.a1a.id, org.a1.id},
    )


def test_any_grant_counts_whatever_its_capabilities(session, org):
    user = _user(session, grants=[(ScopeType.folder, org.b1.id, [])])
    assert _reach(session, user) == ({org.b.id}, {org.b1.id, org.b1a.id})


def test_grants_on_scopes_that_are_gone_or_system_reach_nothing(session, org):
    system = Source(
        name="sys", protocol=Protocol.local, host="localhost", base_path="/", is_system=True
    )
    session.add(system)
    session.commit()
    user = _user(
        session,
        grants=[
            (ScopeType.folder, 9999, VIEW),
            (ScopeType.source, 9999, VIEW),
            (ScopeType.source, system.id, VIEW),
        ],
    )
    assert _reach(session, user) == (set(), set())


def test_a_customer_grant_reaches_a_customer_with_no_folders(session, org):
    user = _user(session, grants=[(ScopeType.customer, org.c.id, VIEW)])
    assert _reach(session, user) == ({org.c.id}, set())


# --- through the app ------------------------------------------------------------


@pytest.fixture()
def real_app(session):
    app.dependency_overrides[get_session] = lambda: session
    yield
    app.dependency_overrides.pop(get_session, None)


def _client(session, user) -> TestClient:
    _, token = create_session(session, user, user_agent="pytest")
    client = TestClient(app)
    client.cookies.set("perchtail_session", token)
    return client


def _denials(session) -> list[AuditLog]:
    session.expire_all()
    return list(session.exec(select(AuditLog).where(AuditLog.action == "access.denied")).all())


def _creator(session, grants) -> User:
    return _user(session, capabilities=[GlobalCapability.create_source], grants=grants)


def _names(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["name"] for item in response.json()}


def test_super_admin_lists_and_changes_every_customer_and_folder(session, real_app, org):
    client = _client(session, _user(session, super_admin=True))

    assert _names(client.get("/customers")) == {"A", "B", "C"}
    assert len(client.get("/folders").json()) == 6
    assert client.post("/customers", json={"name": "D"}).status_code == 201
    assert client.patch(f"/customers/{org.b.id}", json={"name": "B2"}).status_code == 200
    assert client.delete(f"/customers/{org.c.id}").status_code == 204
    assert client.patch(f"/folders/{org.b1.id}", json={"name": "B1x"}).status_code == 200
    assert _denials(session) == []


def test_a_creator_sees_and_changes_only_its_customer(session, real_app, org):
    user = _creator(session, [(ScopeType.customer, org.c.id, MANAGE)])
    client = _client(session, user)

    assert _names(client.get("/customers")) == {"C"}
    assert client.get("/folders").json() == []
    assert client.get(f"/folders?customer_id={org.a.id}").json() == []

    renamed = client.patch(f"/customers/{org.c.id}", json={"name": "C2"})
    assert renamed.status_code == 200, renamed.text
    assert client.delete(f"/customers/{org.c.id}").status_code == 204


def test_a_customer_outside_the_reach_answers_404_and_is_audited(session, real_app, org):
    user = _creator(session, [(ScopeType.customer, org.c.id, MANAGE)])
    client = _client(session, user)

    missing = client.patch("/customers/9999", json={"name": "x"})
    hidden = client.patch(f"/customers/{org.a.id}", json={"name": "x"})
    assert missing.status_code == hidden.status_code == 404
    # The same answer as a customer that doesn't exist.
    assert hidden.json() == missing.json()
    assert client.delete(f"/customers/{org.b.id}").status_code == 404
    session.refresh(org.a)
    assert org.a.name == "A"
    assert session.get(Customer, org.b.id) is not None

    denials = _denials(session)
    assert [(d.target_type, d.target_id) for d in denials] == [
        ("customer", org.a.id),
        ("customer", org.b.id),
    ]
    assert {d.event_metadata["reason"] for d in denials} == {"customer_out_of_reach"}
    assert denials[0].user_id == user.id
    assert denials[0].event_metadata["attempted"] == "PATCH /customers/{customer_id}"


def test_only_a_super_admin_adds_a_customer(session, real_app, org):
    user = _creator(session, [(ScopeType.customer, org.a.id, MANAGE)])
    client = _client(session, user)

    assert client.post("/customers", json={"name": "New"}).status_code == 403
    assert session.exec(select(Customer).where(Customer.name == "New")).first() is None
    [denial] = _denials(session)
    assert denial.event_metadata["reason"] == "super_admin_required"


def test_changing_a_customer_needs_manage_source_on_the_customer_itself(session, real_app, org):
    view_only = _client(session, _creator(session, [(ScopeType.customer, org.a.id, VIEW)]))
    assert view_only.patch(f"/customers/{org.a.id}", json={"name": "x"}).status_code == 403
    assert view_only.delete(f"/customers/{org.c.id}").status_code == 404

    # manage_source on one of its folders covers that folder, not the customer.
    folder_manager = _client(session, _creator(session, [(ScopeType.folder, org.a1.id, MANAGE)]))
    assert _names(folder_manager.get("/customers")) == {"A"}
    assert folder_manager.patch(f"/customers/{org.a.id}", json={"name": "x"}).status_code == 403
    session.refresh(org.a)
    assert org.a.name == "A"


def test_a_folder_grant_lists_that_folder_and_below(session, real_app, org):
    client = _client(session, _creator(session, [(ScopeType.folder, org.a1a.id, VIEW)]))

    assert _names(client.get("/customers")) == {"A"}
    assert _names(client.get("/folders")) == {"A1a", "A1a_i"}
    assert _names(client.get(f"/folders?customer_id={org.a.id}")) == {"A1a", "A1a_i"}


def test_a_source_grant_lists_the_folders_on_the_path_to_it(session, real_app, org):
    client = _client(session, _creator(session, [(ScopeType.source, org.deep.id, VIEW)]))
    assert _names(client.get("/folders")) == {"A1", "A1a", "A1a_i"}


def test_a_folder_outside_the_reach_answers_404(session, real_app, org):
    user = _creator(session, [(ScopeType.folder, org.a1a.id, MANAGE)])
    client = _client(session, user)

    assert client.patch(f"/folders/{org.a2.id}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/folders/{org.b1a.id}").status_code == 404
    # Moving into a folder outside the reach: as if it didn't exist.
    moved = client.patch(f"/folders/{org.a1a_i.id}", json={"parent_folder_id": org.a2.id})
    assert moved.status_code == 404
    assert moved.json()["detail"] == "Parent folder not found"
    created = client.post(
        "/folders", json={"name": "n", "customer_id": org.a.id, "parent_folder_id": org.b1.id}
    )
    assert created.status_code == 404
    elsewhere = client.post("/folders", json={"name": "n", "customer_id": org.b.id})
    assert elsewhere.status_code == 404

    reasons = [(d.target_type, d.event_metadata["reason"]) for d in _denials(session)]
    assert reasons == [
        ("folder", "folder_out_of_reach"),
        ("folder", "folder_out_of_reach"),
        ("folder", "folder_out_of_reach"),
        (None, "folder_out_of_reach"),
        (None, "customer_out_of_reach"),
    ]
    session.refresh(org.a2)
    assert org.a2.name == "A2"


def test_renaming_a_folder_needs_manage_source_on_it(session, real_app, org):
    view_only = _client(session, _creator(session, [(ScopeType.folder, org.a1.id, VIEW)]))
    assert view_only.patch(f"/folders/{org.a1a.id}", json={"name": "x"}).status_code == 403

    manager = _client(session, _creator(session, [(ScopeType.folder, org.a1.id, MANAGE)]))
    response = manager.patch(f"/folders/{org.a1a.id}", json={"name": "renamed"})
    assert response.status_code == 200, response.text


def test_manage_roles_without_create_source_lists_its_reach(session, real_app, org):
    user = _user(
        session,
        capabilities=[GlobalCapability.manage_roles],
        grants=[(ScopeType.folder, org.b1.id, VIEW)],
    )
    client = _client(session, user)

    assert _names(client.get("/customers")) == {"B"}
    assert _names(client.get("/folders")) == {"B1", "B1a"}
    # Listing only: changing customers and folders still needs create_source.
    assert client.patch(f"/customers/{org.b.id}", json={"name": "x"}).status_code == 403
    assert client.patch(f"/folders/{org.b1.id}", json={"name": "x"}).status_code == 403
    assert client.post("/customers", json={"name": "x"}).status_code == 403


def test_a_user_without_either_capability_cannot_list(session, real_app, org):
    client = _client(session, _user(session, grants=[(ScopeType.customer, org.a.id, VIEW)]))
    assert client.get("/customers").status_code == 403
    assert client.get("/folders").status_code == 403


def test_a_creator_without_grants_lists_nothing(session, real_app, org):
    client = _client(session, _creator(session, []))
    assert client.get("/customers").json() == []
    assert client.get("/folders").json() == []


# --- one grant per role and scope -------------------------------------------------


def test_a_second_grant_for_the_same_scope_is_refused(session, real_app, org):
    client = _client(session, _user(session, super_admin=True))
    role = client.post("/roles", json={"name": "ops"}).json()

    body = {"scope_type": "customer", "scope_id": org.a.id, "capabilities": ["view"]}
    assert client.post(f"/roles/{role['id']}/grants", json=body).status_code == 201
    again = client.post(f"/roles/{role['id']}/grants", json={**body, "capabilities": ["download"]})
    assert again.status_code == 409
    assert again.json()["detail"] == "This role already has a grant for that scope; edit it instead"
    grants = session.exec(select(RoleGrant).where(RoleGrant.role_id == role["id"])).all()
    assert [g.capabilities for g in grants] == [["view"]]

    # Another scope, or the same scope on another kind, is fine.
    other = {"scope_type": "folder", "scope_id": org.a.id, "capabilities": ["view"]}
    assert client.post(f"/roles/{role['id']}/grants", json=other).status_code == 201


def test_a_concurrent_duplicate_grant_also_answers_409(session, real_app, org, monkeypatch):
    client = _client(session, _user(session, super_admin=True))
    role = client.post("/roles", json={"name": "ops"}).json()
    real_check = roles_api.require_grant_within_authority

    def concurrent_request_lands_first(session_, *args, **kwargs):
        # Another request adds the same grant after this one checked for it.
        session.add(
            RoleGrant(
                role_id=role["id"],
                scope_type=ScopeType.customer,
                scope_id=org.a.id,
                capabilities=[Capability.view],
            )
        )
        session.commit()
        return real_check(session_, *args, **kwargs)

    monkeypatch.setattr(roles_api, "require_grant_within_authority", concurrent_request_lands_first)
    body = {"scope_type": "customer", "scope_id": org.a.id, "capabilities": ["download"]}
    response = client.post(f"/roles/{role['id']}/grants", json=body)

    assert response.status_code == 409
    assert response.json()["detail"] == roles_api.DUPLICATE_GRANT_REFUSED
    session.expire_all()
    grants = session.exec(select(RoleGrant).where(RoleGrant.role_id == role["id"])).all()
    assert [g.capabilities for g in grants] == [["view"]]
    created = session.exec(select(AuditLog).where(AuditLog.action == "role_grant.create")).all()
    assert created == []


# --- scopes outside the reach answer like scopes that don't exist ------------------


def _source_body(**scope) -> dict:
    return {"name": "n", "protocol": "ssh", "host": "h", "base_path": "/var/log", **scope}


def test_adding_a_source_outside_the_reach_answers_like_a_missing_scope(session, real_app, org):
    user = _creator(session, [(ScopeType.customer, org.a.id, MANAGE)])
    client = _client(session, user)

    for field, hidden_id in (("customer_id", org.b.id), ("folder_id", org.b1a.id)):
        missing = client.post("/sources", json=_source_body(**{field: 9999}))
        hidden = client.post("/sources", json=_source_body(**{field: hidden_id}))
        assert missing.status_code == hidden.status_code == 404
        assert hidden.json() == missing.json()
    assert session.exec(select(Source).where(Source.name == "n")).first() is None

    reasons = [(d.event_metadata["reason"], d.user_id) for d in _denials(session)]
    assert reasons == [("customer_out_of_reach", user.id), ("folder_out_of_reach", user.id)]


def test_moving_a_source_outside_the_reach_answers_like_a_missing_scope(session, real_app, org):
    client = _client(session, _creator(session, [(ScopeType.customer, org.a.id, MANAGE)]))

    for move in (
        {"customer_id": org.b.id, "folder_id": None},
        {"customer_id": org.b.id, "folder_id": org.b1.id},
    ):
        missing_move = {k: (9999 if v is not None else None) for k, v in move.items()}
        missing = client.patch(f"/sources/{org.root.id}", json=missing_move)
        hidden = client.patch(f"/sources/{org.root.id}", json=move)
        assert missing.status_code == hidden.status_code == 404
        assert hidden.json() == missing.json()
    session.refresh(org.root)
    assert (org.root.customer_id, org.root.folder_id) == (org.a.id, None)

    # Within the reach, a move still works.
    moved = client.patch(f"/sources/{org.root.id}", json={"folder_id": org.a2.id})
    assert moved.status_code == 200, moved.text


def test_granting_a_scope_outside_the_reach_answers_like_a_missing_scope(session, real_app, org):
    admin = _user(
        session,
        capabilities=[GlobalCapability.manage_roles],
        grants=[(ScopeType.customer, org.a.id, MANAGE)],
    )
    target = _user(session, grants=[(ScopeType.folder, org.a1.id, VIEW)]).role
    client = _client(session, admin)

    for scope_type, hidden_id in (("customer", org.b.id), ("folder", org.b1.id)):
        body = {"scope_type": scope_type, "capabilities": ["view"]}
        missing = client.post(f"/roles/{target.id}/grants", json={**body, "scope_id": 9999})
        hidden = client.post(f"/roles/{target.id}/grants", json={**body, "scope_id": hidden_id})
        assert missing.status_code == hidden.status_code == 404
        assert hidden.json() == missing.json()
    session.expire_all()
    grants = session.exec(select(RoleGrant).where(RoleGrant.role_id == target.id)).all()
    assert [(g.scope_type, g.scope_id) for g in grants] == [(ScopeType.folder, org.a1.id)]

    reasons = [d.event_metadata["reason"] for d in _denials(session)]
    assert reasons == ["customer_out_of_reach", "folder_out_of_reach"]

    inside = {"scope_type": "folder", "scope_id": org.a2.id, "capabilities": ["view"]}
    assert client.post(f"/roles/{target.id}/grants", json=inside).status_code == 201


# --- a role's grants only within the reader's authority ---------------------------


def test_a_delegated_admin_reads_only_grants_of_roles_within_its_authority(session, real_app, org):
    admin = _user(
        session,
        capabilities=[GlobalCapability.manage_roles],
        grants=[(ScopeType.customer, org.a.id, MANAGE)],
    )
    inside = _user(session, grants=[(ScopeType.folder, org.a1.id, VIEW)]).role
    outside = _user(session, grants=[(ScopeType.customer, org.b.id, VIEW)]).role
    client = _client(session, admin)

    readable = client.get(f"/roles/{inside.id}/grants")
    assert readable.status_code == 200
    assert [g["scope_id"] for g in readable.json()] == [org.a1.id]

    hidden = client.get(f"/roles/{outside.id}/grants")
    missing = client.get("/roles/9999/grants")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    [denial] = _denials(session)
    assert (denial.target_type, denial.target_id) == ("role", outside.id)
    assert denial.event_metadata["reason"] == "role_beyond_authority"

    # The roles list says nothing about grants.
    listed = client.get("/roles")
    assert listed.status_code == 200
    assert all("grants" not in role for role in listed.json())

    super_admin = _client(session, _user(session, super_admin=True))
    assert super_admin.get(f"/roles/{outside.id}/grants").status_code == 200


@pytest.mark.parametrize(
    "capability",
    [GlobalCapability.manage_roles, GlobalCapability.manage_users, GlobalCapability.manage_sso],
)
def test_a_delegated_admin_sees_only_roles_within_its_authority(session, real_app, org, capability):
    admin = _user(
        session, capabilities=[capability], grants=[(ScopeType.customer, org.a.id, MANAGE)]
    )
    inside = _user(session, grants=[(ScopeType.folder, org.a1.id, VIEW)]).role
    outside = _user(session, grants=[(ScopeType.customer, org.b.id, VIEW)]).role
    wider = _user(
        session,
        capabilities=[GlobalCapability.view_audit_log],
        grants=[(ScopeType.folder, org.a1.id, VIEW)],
    ).role
    super_role = _user(session, super_admin=True).role
    client = _client(session, admin)

    listed = client.get("/roles")
    assert listed.status_code == 200
    ids = {role["id"] for role in listed.json()}
    assert {inside.id, admin.role_id} <= ids
    assert ids.isdisjoint({outside.id, wider.id, super_role.id})

    assert client.get(f"/roles/{inside.id}").status_code == 200
    missing = client.get("/roles/9999")
    for hidden_role in (outside, wider, super_role):
        hidden = client.get(f"/roles/{hidden_role.id}")
        assert missing.status_code == hidden.status_code == 404
        assert hidden.json() == missing.json()
    denials = _denials(session)
    assert [(d.target_type, d.target_id) for d in denials] == [
        ("role", outside.id),
        ("role", wider.id),
        ("role", super_role.id),
    ]
    assert {d.event_metadata["reason"] for d in denials} == {"role_beyond_authority"}


def test_a_super_admin_sees_every_role(session, real_app, org):
    outside = _user(session, grants=[(ScopeType.customer, org.b.id, VIEW)]).role
    client = _client(session, _user(session, super_admin=True))

    ids = {role["id"] for role in client.get("/roles").json()}
    assert set(session.exec(select(Role.id)).all()) == ids
    assert client.get(f"/roles/{outside.id}").status_code == 200


# --- the audit log API turned off with its page -----------------------------------

AUDIT_READS = [
    ("GET", "/audit"),
    ("GET", "/audit/filters"),
    ("GET", "/audit/integrity"),
    ("POST", "/audit/integrity/verify"),
]


def _turn_audit_view(session, enabled: bool) -> None:
    set_bool(session, "audit_view_enabled", enabled)
    session.commit()


@pytest.mark.parametrize(("method", "path"), AUDIT_READS)
def test_turned_off_the_audit_log_answers_404_to_all_but_a_super_admin(
    session, real_app, method, path
):
    _turn_audit_view(session, False)
    auditor = _client(session, _user(session, capabilities=[GlobalCapability.view_audit_log]))
    nobody = _client(session, _user(session))
    super_admin = _client(session, _user(session, super_admin=True))

    assert auditor.request(method, path).status_code == 404
    assert nobody.request(method, path).status_code == 404
    assert super_admin.request(method, path).status_code == 200
    reasons = {d.event_metadata["reason"] for d in _denials(session)}
    assert reasons == {"audit_view_disabled"}


@pytest.mark.parametrize(("method", "path"), AUDIT_READS)
def test_turned_on_the_audit_log_answers_to_view_audit_log(session, real_app, method, path):
    _turn_audit_view(session, True)
    auditor = _client(session, _user(session, capabilities=[GlobalCapability.view_audit_log]))
    nobody = _client(session, _user(session))

    assert auditor.request(method, path).status_code == 200
    assert nobody.request(method, path).status_code == 403
