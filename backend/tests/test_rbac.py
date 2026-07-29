from app.auth.models import Capability, Role, RoleGrant, ScopeType, User
from app.auth.rbac import create_role, create_role_grant, require_capability, resolve_capability
from app.db import get_session
from app.models import Customer, Folder, Protocol, Source
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def _make_role(session, *, is_super_admin=False) -> Role:
    role = Role(name=f"role-{is_super_admin}", is_super_admin=is_super_admin)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def _make_user(session, role: Role) -> User:
    user = User(username=f"user-{role.id}@example.com", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _make_customer_source(session, *, is_system=False) -> tuple[Customer, Source]:
    customer = Customer(name="Vodacom Tanzania")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    source = Source(
        name="app01",
        customer_id=None if is_system else customer.id,
        protocol=Protocol.local if is_system else Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log/appname",
        is_system=is_system,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return customer, source


def _make_folder_chain(session, customer: Customer, depth: int) -> list[Folder]:
    """Creates `depth` nested folders under customer, root first."""
    folders = []
    parent_id = None
    for i in range(depth):
        folder = Folder(name=f"folder-{i}", customer_id=customer.id, parent_folder_id=parent_id)
        session.add(folder)
        session.commit()
        session.refresh(folder)
        folders.append(folder)
        parent_id = folder.id
    return folders


def test_super_admin_allowed_without_any_grant(session):
    role = _make_role(session, is_super_admin=True)
    user = _make_user(session, role)
    _, source = _make_customer_source(session)

    assert resolve_capability(session, user, source, Capability.view) is True


def test_non_super_admin_denied_without_grant(session):
    role = _make_role(session)
    user = _make_user(session, role)
    _, source = _make_customer_source(session)

    assert resolve_capability(session, user, source, Capability.view) is False


def test_system_source_requires_super_admin_regardless_of_grants(session):
    role = _make_role(session)
    user = _make_user(session, role)
    _, source = _make_customer_source(session, is_system=True)

    grant = RoleGrant(
        role_id=role.id,
        scope_type=ScopeType.customer,
        scope_id=999999,
        capabilities=[Capability.view],
    )
    session.add(grant)
    session.commit()

    assert resolve_capability(session, user, source, Capability.view) is False

    role.is_super_admin = True
    session.add(role)
    session.commit()
    assert resolve_capability(session, user, source, Capability.view) is True


def test_customer_scope_grant_applies_to_its_sources(session):
    role = _make_role(session)
    user = _make_user(session, role)
    customer, source = _make_customer_source(session)

    grant = RoleGrant(
        role_id=role.id,
        scope_type=ScopeType.customer,
        scope_id=customer.id,
        capabilities=[Capability.view],
    )
    session.add(grant)
    session.commit()

    assert resolve_capability(session, user, source, Capability.view) is True
    assert resolve_capability(session, user, source, Capability.download) is False


def test_source_scope_grant_overrides_customer_scope(session):
    role = _make_role(session)
    user = _make_user(session, role)
    customer, source = _make_customer_source(session)

    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.customer,
            scope_id=customer.id,
            capabilities=[Capability.view],
        )
    )
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=[],
        )
    )
    session.commit()

    # The source-scoped grant exists (even with no capabilities) and wins
    # over the customer-scoped one, per CLAUDE.md's grant resolution order.
    assert resolve_capability(session, user, source, Capability.view) is False


def test_source_with_no_folder_falls_through_to_customer_grant(session):
    role = _make_role(session)
    user = _make_user(session, role)
    customer, source = _make_customer_source(session)
    assert source.folder_id is None

    create_role_grant(
        session,
        actor_user_id=None,
        role_id=role.id,
        scope_type=ScopeType.customer,
        scope_id=customer.id,
        capabilities=[Capability.view],
    )

    assert resolve_capability(session, user, source, Capability.view) is True


def test_folder_scope_grant_applies_to_sources_several_levels_deep(session):
    role = _make_role(session)
    user = _make_user(session, role)
    customer, source = _make_customer_source(session)

    folders = _make_folder_chain(session, customer, depth=3)
    source.folder_id = folders[-1].id  # deepest folder
    session.add(source)
    session.commit()

    create_role_grant(
        session,
        actor_user_id=None,
        role_id=role.id,
        scope_type=ScopeType.folder,
        scope_id=folders[0].id,  # grant on the ROOT folder, several levels up
        capabilities=[Capability.view],
    )

    assert resolve_capability(session, user, source, Capability.view) is True
    assert resolve_capability(session, user, source, Capability.download) is False


def test_nearest_folder_grant_wins_over_ancestor_folder_grant(session):
    role = _make_role(session)
    user = _make_user(session, role)
    customer, source = _make_customer_source(session)

    folders = _make_folder_chain(session, customer, depth=2)
    source.folder_id = folders[-1].id
    session.add(source)
    session.commit()

    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.folder,
            scope_id=folders[0].id,  # ancestor: would allow
            capabilities=[Capability.view],
        )
    )
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.folder,
            scope_id=folders[-1].id,  # nearest: explicitly empty (deny)
            capabilities=[],
        )
    )
    session.commit()

    assert resolve_capability(session, user, source, Capability.view) is False


def test_folder_scope_grant_overrides_customer_scope(session):
    role = _make_role(session)
    user = _make_user(session, role)
    customer, source = _make_customer_source(session)

    folder = _make_folder_chain(session, customer, depth=1)[0]
    source.folder_id = folder.id
    session.add(source)
    session.commit()

    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.customer,
            scope_id=customer.id,
            capabilities=[Capability.view],
        )
    )
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.folder,
            scope_id=folder.id,
            capabilities=[],
        )
    )
    session.commit()

    assert resolve_capability(session, user, source, Capability.view) is False


def test_source_scope_grant_overrides_folder_scope(session):
    role = _make_role(session)
    user = _make_user(session, role)
    customer, source = _make_customer_source(session)

    folder = _make_folder_chain(session, customer, depth=1)[0]
    source.folder_id = folder.id
    session.add(source)
    session.commit()

    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.folder,
            scope_id=folder.id,
            capabilities=[Capability.view],
        )
    )
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=[],
        )
    )
    session.commit()

    assert resolve_capability(session, user, source, Capability.view) is False


def test_require_capability_dependency_allows_and_denies(session):
    role = _make_role(session)
    user = _make_user(session, role)
    customer, source = _make_customer_source(session)
    create_role_grant(
        session,
        actor_user_id=None,
        role_id=role.id,
        scope_type=ScopeType.customer,
        scope_id=customer.id,
        capabilities=[Capability.view],
    )

    app = FastAPI()
    app.dependency_overrides[get_session] = lambda: session

    def fake_current_user():
        return user

    @app.get("/sources/{source_id}/view")
    def view_source(
        source: Source = Depends(require_capability(Capability.view, fake_current_user)),
    ):
        return {"id": source.id}

    @app.get("/sources/{source_id}/download")
    def download_source(
        source: Source = Depends(require_capability(Capability.download, fake_current_user)),
    ):
        return {"id": source.id}

    client = TestClient(app)

    ok = client.get(f"/sources/{source.id}/view")
    assert ok.status_code == 200

    forbidden = client.get(f"/sources/{source.id}/download")
    assert forbidden.status_code == 403

    not_found = client.get("/sources/999999/view")
    assert not_found.status_code == 404


def test_create_role_and_grant_write_audit_log(session):
    from app.auth.models import AuditLog
    from sqlmodel import select

    role = create_role(session, actor_user_id=None, name="Tier 2", global_capabilities=[])
    grant = create_role_grant(
        session,
        actor_user_id=None,
        role_id=role.id,
        scope_type=ScopeType.customer,
        scope_id=1,
        capabilities=[Capability.view],
    )

    actions = {entry.action for entry in session.exec(select(AuditLog)).all()}
    assert "role.create" in actions
    assert "role_grant.create" in actions
    assert grant.role_id == role.id
