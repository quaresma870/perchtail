from app.auth.models import (
    AuditLog,
    AuthProviderType,
    Capability,
    GlobalCapability,
    Role,
    RoleGrant,
    ScopeType,
    User,
)
from app.models import Customer, Folder, PatternKind, Protocol, Rule, RuleType, Source


def test_customer_source_rule_relationships(session):
    customer = Customer(name="Vodacom Tanzania")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    source = Source(
        name="app01",
        customer_id=customer.id,
        protocol=Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log/appname",
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    rule = Rule(
        source_id=source.id,
        order=0,
        type=RuleType.include,
        pattern="**/*.log",
        pattern_kind=PatternKind.glob,
    )
    session.add(rule)
    session.commit()

    session.refresh(customer)
    session.refresh(source)
    assert customer.sources == [source]
    assert source.rules[0].pattern == "**/*.log"
    assert source.rules[0].pattern_kind == PatternKind.glob


def test_source_defaults_and_system_source():
    source = Source(
        name="perchtail-logs",
        protocol=Protocol.local,
        host="localhost",
        base_path="/data/logs",
        is_system=True,
    )
    assert source.customer_id is None
    assert source.credential_ref is None
    assert source.enabled is True
    assert source.is_system is True


def test_role_grant_capabilities_roundtrip(session):
    role = Role(
        name="Tier 2 — Vodacom, view only",
        global_capabilities=[GlobalCapability.create_source],
    )
    session.add(role)
    session.commit()
    session.refresh(role)

    grant = RoleGrant(
        role_id=role.id,
        scope_type=ScopeType.customer,
        scope_id=1,
        capabilities=[Capability.view, Capability.download],
    )
    session.add(grant)
    session.commit()
    session.refresh(grant)

    assert Capability.view in grant.capabilities
    assert Capability.download in grant.capabilities
    assert Capability.manage_rules not in grant.capabilities
    assert GlobalCapability.create_source in role.global_capabilities


def test_user_role_relationship(session):
    role = Role(name="Support", is_super_admin=False)
    session.add(role)
    session.commit()
    session.refresh(role)

    user = User(
        username="jdoe@example.com",
        role_id=role.id,
        auth_provider=AuthProviderType.local,
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    assert user.role_id == role.id
    assert user.active is True
    assert user.password_hash is None


def test_folder_nests_arbitrarily_deep_and_groups_sources(session):
    customer = Customer(name="Vodacom Tanzania")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    root = Folder(name="Production", customer_id=customer.id)
    session.add(root)
    session.commit()
    session.refresh(root)

    child = Folder(name="App Servers", customer_id=customer.id, parent_folder_id=root.id)
    session.add(child)
    session.commit()
    session.refresh(child)

    source = Source(
        name="app01",
        customer_id=customer.id,
        folder_id=child.id,
        protocol=Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log/appname",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    session.refresh(root)
    session.refresh(child)

    assert child.parent_folder == root
    assert root.child_folders == [child]
    assert child.sources == [source]
    assert source.folder == child


def test_source_folder_id_defaults_to_none():
    source = Source(
        name="app01",
        protocol=Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log/appname",
    )
    assert source.folder_id is None


def test_audit_log_metadata_column_roundtrip(session):
    entry = AuditLog(
        action="source.create",
        target_type="source",
        target_id=1,
        event_metadata={"name": "app01"},
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)

    assert entry.event_metadata == {"name": "app01"}
    assert entry.timestamp is not None
