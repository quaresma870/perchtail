"""#239: audit entries name what they're about, also after a delete."""

import ast
from pathlib import Path

from app.audit import record_audit_event
from app.audit_labels import NAMED_TARGET_TYPES, UNNAMED_TARGET_TYPES
from app.auth.models import Role, RoleGrant, ScopeType, SSOGroupRoleMapping, User
from app.models import Customer, Folder, Protocol, Rule, RuleType, SeverityPattern, Source


def _add(session, row):
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _tree(session):
    customer = _add(session, Customer(name="Vodacom"))
    prod = _add(session, Folder(name="prod", customer_id=customer.id))
    web = _add(session, Folder(name="web", customer_id=customer.id, parent_folder_id=prod.id))
    source = _add(
        session,
        Source(
            name="app01",
            protocol=Protocol.ssh,
            host="app01",
            base_path="/var/log",
            customer_id=customer.id,
            folder_id=web.id,
        ),
    )
    return customer, prod, web, source


def _event(session, **kwargs):
    entry = record_audit_event(session, user_id=None, action="test.action", **kwargs)
    session.commit()
    return entry.event_metadata


def test_a_source_is_named_with_its_customer_and_folders(session):
    _, _, _, source = _tree(session)
    assert _event(session, target_type="source", target_id=source.id) == {
        "target_name": "app01",
        "target_context": "Vodacom / prod / web",
    }


def test_a_folder_is_named_with_its_customer_and_parents(session):
    _, _, web, _ = _tree(session)
    assert _event(session, target_type="folder", target_id=web.id) == {
        "target_name": "web",
        "target_context": "Vodacom / prod",
    }


def test_a_deleted_source_is_still_named(session):
    """Delete endpoints record their event right after session.delete()."""
    _, _, _, source = _tree(session)
    source_id = source.id
    session.delete(source)
    metadata = _event(session, target_type="source", target_id=source_id)
    assert metadata["target_name"] == "app01"


def test_a_deleted_customer_and_user_are_still_named(session):
    customer = _add(session, Customer(name="Fidelidade"))
    role = _add(session, Role(name="viewer"))
    user = _add(session, User(username="ana@example.com", role_id=role.id))
    customer_id, user_id = customer.id, user.id
    session.delete(customer)
    assert _event(session, target_type="customer", target_id=customer_id) == {
        "target_name": "Fidelidade"
    }
    session.delete(user)
    assert _event(session, target_type="user", target_id=user_id) == {
        "target_name": "ana@example.com"
    }


def test_rules_grants_and_mappings_are_named_with_their_parent(session):
    _, prod, _, source = _tree(session)
    role = _add(session, Role(name="Tier 2"))
    rule = _add(
        session, Rule(source_id=source.id, order=0, type=RuleType.include, pattern="**/*.log")
    )
    grant = _add(
        session,
        RoleGrant(role_id=role.id, scope_type=ScopeType.folder, scope_id=prod.id, capabilities=[]),
    )
    mapping = _add(session, SSOGroupRoleMapping(group_name="ops", role_id=role.id, order=0))
    pattern = _add(session, SeverityPattern(level="error", pattern="ERROR"))

    assert _event(session, target_type="rule", target_id=rule.id) == {
        "target_name": "**/*.log",
        "target_context": "app01",
    }
    assert _event(session, target_type="role_grant", target_id=grant.id) == {
        "target_name": "folder prod",
        "target_context": "Tier 2",
    }
    assert _event(session, target_type="sso_group_mapping", target_id=mapping.id) == {
        "target_name": "ops",
        "target_context": "Tier 2",
    }
    assert _event(session, target_type="severity_pattern", target_id=pattern.id) == {
        "target_name": "ERROR",
        "target_context": "global",
    }


def test_related_ids_get_their_names_and_explicit_metadata_wins(session):
    customer, prod, _, source = _tree(session)
    role = _add(session, Role(name="Tier 2"))
    metadata = _event(
        session,
        target_type="role",
        target_id=role.id,
        metadata={
            "source_id": source.id,
            "role_id": role.id,
            "customer_id": customer.id,
            "scope_type": "folder",
            "scope_id": prod.id,
            "target_name": "given by the caller",
        },
    )
    assert metadata["target_name"] == "given by the caller"
    assert (metadata["source_name"], metadata["role_name"]) == ("app01", "Tier 2")
    assert (metadata["customer_name"], metadata["scope_name"]) == ("Vodacom", "prod")


def test_an_unknown_target_adds_nothing(session):
    assert _event(session, target_type="source", target_id=999) is None
    assert _event(session, target_type="system_settings", metadata={"x": 1}) == {"x": 1}


def _target_types_in_audit_calls() -> set[str]:
    app_dir = Path(__file__).resolve().parent.parent / "app"
    found: set[str] = set()
    for path in app_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "record_audit_event":
                continue
            for keyword in node.keywords:
                if keyword.arg == "target_type":
                    value = keyword.value
                    # `"source" if ... else None` counts as "source".
                    if isinstance(value, ast.IfExp):
                        value = value.body
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.add(value.value)
    return found


def test_every_audited_target_type_gets_a_name():
    """A new kind of target must be added to app/audit_labels.py (or listed
    there as having no id), or its events go back to bare ids."""
    used = _target_types_in_audit_calls()
    assert "source" in used and "user" in used  # the scan works
    assert used - NAMED_TARGET_TYPES - UNNAMED_TARGET_TYPES == set()
