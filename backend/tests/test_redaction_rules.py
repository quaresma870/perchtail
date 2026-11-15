"""Redaction rules (#382, #249): model, effective rules, API and audit."""

import re
import uuid

import pytest
from app import system_settings
from app.api.auth import get_current_active_user
from app.api.redaction_rules import global_router, source_router
from app.api.system_settings import router as system_settings_router
from app.auth.models import AuditLog, Capability, GlobalCapability, Role, RoleGrant, ScopeType, User
from app.db import get_session
from app.models import Customer, Protocol, RedactionRule, Source, SourceRedactionOverride
from app.redaction import DETECTOR_NAMES, redact_bytes
from app.redaction_rules import (
    MAX_RULES_PER_SCOPE,
    RedactionRuleInvalid,
    check_custom_rule,
    delete_source_redaction,
    effective_redaction_rules,
    engine_rules,
    pattern_max_bytes,
    redaction_applies,
    seed_builtin_redaction_rules,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

MSISDN = {"name": "msisdn", "pattern": r"\+255(\d{9})", "secret_group": 1, "max_length": 13}


def _user(session, *, super_admin=False, global_capabilities=None) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=super_admin,
        global_capabilities=global_capabilities or [],
    )
    session.add(role)
    session.commit()
    user = User(username=f"user-{uuid.uuid4().hex}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _source(session, name="app01") -> Source:
    customer = Customer(name=f"Acme {uuid.uuid4().hex}")
    session.add(customer)
    session.commit()
    source = Source(
        name=name, customer_id=customer.id, protocol=Protocol.ssh, host=name, base_path="/var/log"
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@pytest.fixture()
def client_for(session):
    def _make(user):
        app = FastAPI()
        app.include_router(global_router)
        app.include_router(source_router)
        app.include_router(system_settings_router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return _make


@pytest.fixture()
def admin(session, client_for):
    seed_builtin_redaction_rules(session)
    return client_for(_user(session, super_admin=True))


def _builtin(session, detector) -> RedactionRule:
    return session.exec(
        select(RedactionRule).where(
            RedactionRule.source_id.is_(None), RedactionRule.detector == detector
        )
    ).one()


def _audit(session, action) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


# --- seed --------------------------------------------------------------------


def test_seed_adds_the_built_ins_disabled_and_is_idempotent(session):
    seed_builtin_redaction_rules(session)
    seed_builtin_redaction_rules(session)
    rules = session.exec(select(RedactionRule)).all()
    assert sorted(r.detector for r in rules) == sorted(DETECTOR_NAMES)
    assert all(r.source_id is None and not r.enabled and r.name == r.detector for r in rules)
    assert [r.order for r in sorted(rules, key=lambda r: r.order)] == list(
        range(len(DETECTOR_NAMES))
    )


def test_seed_adds_only_missing_detectors(session):
    seed_builtin_redaction_rules(session)
    card = _builtin(session, "card")
    card.enabled = True
    session.add(card)
    session.delete(_builtin(session, "iban"))
    session.commit()
    seed_builtin_redaction_rules(session)
    assert _builtin(session, "card").enabled
    assert not _builtin(session, "iban").enabled


# --- effective rules and redaction_applies -------------------------------------


def test_effective_rules_join_system_and_source_rules(session):
    seed_builtin_redaction_rules(session)
    source, other = _source(session), _source(session, "app02")
    for detector in ("card", "email", "ipv4"):
        rule = _builtin(session, detector)
        rule.enabled = True
        session.add(rule)
    session.add(SourceRedactionOverride(source_id=source.id, rule_id=_builtin(session, "ipv4").id))
    session.add(RedactionRule(source_id=source.id, enabled=True, order=1, **MSISDN))
    session.add(
        RedactionRule(
            source_id=source.id, enabled=True, order=0, name="ref", pattern="REF\\d+", max_length=20
        )
    )
    session.add(
        RedactionRule(source_id=source.id, enabled=False, name="off", pattern="x", max_length=1)
    )
    session.commit()
    assert [r.name for r in effective_redaction_rules(session, source.id)] == [
        "card",
        "email",
        "ref",
        "msisdn",
    ]
    assert [r.name for r in effective_redaction_rules(session, other.id)] == [
        "card",
        "email",
        "ipv4",
    ]


def test_engine_rules_run_in_the_engine(session):
    seed_builtin_redaction_rules(session)
    source = _source(session)
    card = _builtin(session, "card")
    card.enabled = True
    session.add(card)
    session.add(RedactionRule(source_id=source.id, enabled=True, **MSISDN))
    session.commit()
    rules = engine_rules(effective_redaction_rules(session, source.id))
    assert rules[0] == {"id": card.id, "name": "card", "detector": "card"}
    assert rules[1]["max_length"] == 13
    out, result = redact_bytes(b"call +255712345678 pay 4111 1111 1111 1111\n", rules)
    assert out == b"call +255[redacted: msisdn] pay [redacted: card ****1111]\n"
    assert result.counts == {"card": 1, "msisdn": 1}


def test_redaction_applies_needs_the_switch_and_a_rule_for_everyone(session):
    seed_builtin_redaction_rules(session)
    source = _source(session)
    assert not redaction_applies(session, source)
    system_settings.set_bool(session, "redaction_enabled", True)
    session.commit()
    # On with no enabled rule: nothing changes.
    assert not redaction_applies(session, source)
    card = _builtin(session, "card")
    card.enabled = True
    session.add(card)
    session.commit()
    assert redaction_applies(session, source)
    # A source that turns the only rule off isn't redacted.
    session.add(SourceRedactionOverride(source_id=source.id, rule_id=card.id))
    session.commit()
    assert not redaction_applies(session, source)
    session.add(RedactionRule(source_id=source.id, enabled=True, **MSISDN))
    session.commit()
    assert redaction_applies(session, source)
    system_settings.set_bool(session, "redaction_enabled", False)
    session.commit()
    assert not redaction_applies(session, source)


def test_deleting_a_source_deletes_its_redaction(session):
    seed_builtin_redaction_rules(session)
    source = _source(session)
    session.add(RedactionRule(source_id=source.id, enabled=True, **MSISDN))
    session.add(SourceRedactionOverride(source_id=source.id, rule_id=_builtin(session, "card").id))
    session.commit()
    delete_source_redaction(session, source.id)
    session.commit()
    left = session.exec(select(RedactionRule).where(RedactionRule.source_id == source.id)).all()
    assert left == []
    assert session.exec(select(SourceRedactionOverride)).all() == []


# --- checking custom rules ----------------------------------------------------


@pytest.mark.parametrize(
    "pattern, group, max_length, message",
    [
        ("(a", None, 10, "isn't valid RE2"),
        ("(a)\\1", None, 10, "isn't valid RE2"),
        ("(?=a)b", None, 10, "isn't valid RE2"),
        ("a\\Cb", None, 10, "\\C"),
        ("x*", None, 10, "matches empty text"),
        ("a|", None, 10, "matches empty text"),
        ("ab", 1, 10, "no group 1"),
        ("a" * 1001, None, 4096, "longer than 1000"),
        ("(?:ab|cd){900}", None, 4096, "too complex"),
        ("ACCT(?: \\d{4}){20}", None, 64, "up to 104 bytes"),
        ("é{10}", None, 39, "up to 40 bytes"),
        ("a", None, 0, "max_length"),
        ("a", None, 4097, "max_length"),
    ],
)
def test_custom_rules_that_cant_be_saved(pattern, group, max_length, message):
    with pytest.raises(RedactionRuleInvalid, match=re.escape(message)):
        check_custom_rule(pattern, group, max_length)


@pytest.mark.parametrize(
    "pattern, group, max_length",
    [
        ("ACCT(?: \\d{4}){20}", None, 104),
        (r"\+255(\d{9})", 1, 13),
        # An unbounded repeat can't be measured: the declared value is kept.
        (r"ref=\w+", None, 64),
        # Python's parser doesn't read \pL: nothing to measure.
        (r"\pL{3}x", None, 4),
        (r"\\C", None, 2),
        (r"\bword\b", None, 4),
    ],
)
def test_custom_rules_that_can_be_saved(pattern, group, max_length):
    check_custom_rule(pattern, group, max_length)


def test_pattern_max_bytes():
    assert pattern_max_bytes("ab(cd){2}") == 6
    assert pattern_max_bytes("a+") is None
    assert pattern_max_bytes("é") == 4
    assert pattern_max_bytes("(") is None


# --- API: super admins only -----------------------------------------------------


ENDPOINTS = [
    ("get", "/redaction-rules", None),
    ("post", "/redaction-rules", MSISDN),
    ("post", "/redaction-rules/test", {"pattern": "a", "sample": "a"}),
    ("patch", "/redaction-rules/1", {"enabled": True}),
    ("delete", "/redaction-rules/1", None),
    ("get", "/sources/{sid}/redaction-rules", None),
    ("get", "/sources/{sid}/redaction-rules/effective", None),
    ("post", "/sources/{sid}/redaction-rules", MSISDN),
    ("patch", "/sources/{sid}/redaction-rules/1", {"enabled": True}),
    ("delete", "/sources/{sid}/redaction-rules/1", None),
    ("put", "/sources/{sid}/redaction-overrides/1", {"disabled": True}),
]


@pytest.mark.parametrize(
    "capabilities",
    [
        [],
        [GlobalCapability.manage_system_settings],
        [GlobalCapability.manage_roles, GlobalCapability.create_source],
    ],
)
def test_every_endpoint_refuses_anyone_but_a_super_admin(session, client_for, capabilities):
    seed_builtin_redaction_rules(session)
    source = _source(session)
    user = _user(session, global_capabilities=capabilities)
    # Even manage_rules and manage_source on the source don't reach redaction.
    session.add(
        RoleGrant(
            role_id=user.role_id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=[Capability.view, Capability.manage_rules, Capability.manage_source],
        )
    )
    session.commit()
    client = client_for(user)
    for method, path, body in ENDPOINTS:
        kwargs = {"json": body} if body is not None else {}
        response = getattr(client, method)(path.format(sid=source.id), **kwargs)
        assert response.status_code == 403, (method, path, response.text)
    assert session.exec(select(RedactionRule).where(RedactionRule.detector.is_(None))).all() == []
    assert not any(r.enabled for r in session.exec(select(RedactionRule)).all())


def test_super_admin_lists_and_toggles_built_ins(session, admin):
    listed = admin.get("/redaction-rules").json()
    assert [r["detector"] for r in listed] == list(DETECTOR_NAMES)
    card = listed[0]
    response = admin.patch(f"/redaction-rules/{card['id']}", json={"enabled": True, "notes": "PCI"})
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    entry = _audit(session, "redaction_rule.update")[-1]
    assert entry.target_type == "redaction_rule"
    assert entry.target_id == card["id"]
    assert entry.event_metadata["scope"] == "system"
    assert entry.event_metadata["changes"]["enabled"] == {"old": False, "new": True}
    assert entry.event_metadata["target_name"] == "card"
    assert entry.event_metadata["target_context"] == "system"


def test_built_ins_cant_be_changed_into_something_else_or_deleted(session, admin):
    card = _builtin(session, "card")
    for body in ({"pattern": "x"}, {"name": "visa"}, {"max_length": 10}, {"secret_group": 1}):
        assert admin.patch(f"/redaction-rules/{card.id}", json=body).status_code == 422
    assert admin.delete(f"/redaction-rules/{card.id}").status_code == 422
    assert session.get(RedactionRule, card.id) is not None


def test_create_update_delete_a_system_custom_rule(session, admin):
    response = admin.post("/redaction-rules", json=MSISDN)
    assert response.status_code == 201, response.text
    rule = response.json()
    assert rule["source_id"] is None
    assert rule["detector"] is None
    assert rule["enabled"] is True
    assert rule["order"] == len(DETECTOR_NAMES)
    created = _audit(session, "redaction_rule.create")[-1]
    assert created.event_metadata["pattern"] == MSISDN["pattern"]
    assert created.event_metadata["max_length"] == 13

    response = admin.patch(
        f"/redaction-rules/{rule['id']}",
        json={"pattern": r"\+258(\d{9})", "secret_group": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["secret_group"] is None
    changes = _audit(session, "redaction_rule.update")[-1].event_metadata["changes"]
    assert changes["pattern"] == {"old": MSISDN["pattern"], "new": r"\+258(\d{9})"}

    # The new pattern is checked against the stored max_length.
    response = admin.patch(f"/redaction-rules/{rule['id']}", json={"pattern": r"\+258\d{20}"})
    assert response.status_code == 422

    source = _source(session)
    admin.put(f"/sources/{source.id}/redaction-overrides/{rule['id']}", json={"disabled": True})
    assert admin.delete(f"/redaction-rules/{rule['id']}").status_code == 204
    assert session.get(RedactionRule, rule["id"]) is None
    assert session.exec(select(SourceRedactionOverride)).all() == []
    deleted = _audit(session, "redaction_rule.delete")[-1]
    assert deleted.event_metadata["name"] == "msisdn"
    assert deleted.event_metadata["target_name"] == "msisdn"


@pytest.mark.parametrize(
    "body",
    [
        {**MSISDN, "max_length": 12},
        {**MSISDN, "max_length": 0},
        {**MSISDN, "max_length": 4097},
        {**MSISDN, "pattern": "x*"},
        {**MSISDN, "pattern": "a\\Cb"},
        {**MSISDN, "pattern": "(a"},
        {**MSISDN, "secret_group": 2},
        {**MSISDN, "name": ""},
        {**MSISDN, "name": "a" * 49},
        {**MSISDN, "name": "bad]name"},
        {**MSISDN, "name": "two\nlines"},
        {"name": "x", "pattern": "x"},
    ],
)
def test_invalid_custom_rules_are_refused(session, admin, body):
    assert admin.post("/redaction-rules", json=body).status_code == 422
    assert _audit(session, "redaction_rule.create") == []


def test_names_are_unique_in_their_scope(session, admin):
    assert admin.post("/redaction-rules", json=MSISDN).status_code == 201
    assert admin.post("/redaction-rules", json={**MSISDN, "name": "MSISDN"}).status_code == 409
    assert admin.post("/redaction-rules", json={**MSISDN, "name": "card"}).status_code == 409
    source = _source(session)
    assert admin.post(f"/sources/{source.id}/redaction-rules", json=MSISDN).status_code == 201
    assert admin.post(f"/sources/{source.id}/redaction-rules", json=MSISDN).status_code == 409


def test_at_most_fifty_rules_per_scope(session, admin):
    source = _source(session)
    for i in range(MAX_RULES_PER_SCOPE):
        body = {"name": f"r{i}", "pattern": f"x{i}y", "max_length": 10}
        assert admin.post(f"/sources/{source.id}/redaction-rules", json=body).status_code == 201
    body = {"name": "one-more", "pattern": "zz", "max_length": 10}
    assert admin.post(f"/sources/{source.id}/redaction-rules", json=body).status_code == 422


def test_source_rules_and_overrides(session, admin):
    source, other = _source(session), _source(session, "app02")
    card = _builtin(session, "card")
    email = _builtin(session, "email")
    for rule in (card, email):
        admin.patch(f"/redaction-rules/{rule.id}", json={"enabled": True})

    response = admin.post(f"/sources/{source.id}/redaction-rules", json=MSISDN)
    assert response.status_code == 201
    own = response.json()
    assert own["source_id"] == source.id
    assert _audit(session, "redaction_rule.create")[-1].event_metadata["source_id"] == source.id

    response = admin.put(
        f"/sources/{source.id}/redaction-overrides/{email.id}", json={"disabled": True}
    )
    assert response.status_code == 200
    assert response.json() == {"source_id": source.id, "rule_id": email.id, "disabled": True}
    entry = _audit(session, "redaction_override.set")[-1]
    assert entry.target_id == email.id
    assert entry.event_metadata["source_id"] == source.id
    assert entry.event_metadata["disabled"] is True

    effective = admin.get(f"/sources/{source.id}/redaction-rules/effective").json()
    assert [r["name"] for r in effective] == ["card", "msisdn"]
    assert [r["name"] for r in admin.get(f"/sources/{source.id}/redaction-rules").json()] == [
        "msisdn"
    ]
    other_effective = admin.get(f"/sources/{other.id}/redaction-rules/effective").json()
    assert [r["name"] for r in other_effective] == ["card", "email"]

    # A source rule is only reachable through its own source.
    assert (
        admin.patch(
            f"/sources/{other.id}/redaction-rules/{own['id']}", json={"enabled": False}
        ).status_code
        == 404
    )
    assert admin.patch(f"/redaction-rules/{own['id']}", json={"enabled": False}).status_code == 404
    # Overrides are for system rules only.
    assert (
        admin.put(
            f"/sources/{source.id}/redaction-overrides/{own['id']}", json={"disabled": True}
        ).status_code
        == 404
    )

    admin.put(f"/sources/{source.id}/redaction-overrides/{email.id}", json={"disabled": False})
    assert session.exec(select(SourceRedactionOverride)).all() == []
    effective = admin.get(f"/sources/{source.id}/redaction-rules/effective").json()
    assert [r["name"] for r in effective] == ["card", "email", "msisdn"]

    assert admin.delete(f"/sources/{source.id}/redaction-rules/{own['id']}").status_code == 204
    entry = _audit(session, "redaction_rule.delete")[-1]
    assert entry.event_metadata["scope"] == "source"
    assert entry.event_metadata["source_name"] == source.name


def test_unknown_source_is_404(session, admin):
    assert admin.get("/sources/9999/redaction-rules").status_code == 404
    assert admin.post("/sources/9999/redaction-rules", json=MSISDN).status_code == 404


def test_pattern_test_on_typed_text(session, admin):
    response = admin.post(
        "/redaction-rules/test",
        json={
            "pattern": r"\+255(\d{9})",
            "secret_group": 1,
            "sample": "é call +255712345678\nand +255700000000",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["redacted"] == "é call +255[redacted: test]\nand +255[redacted: test]"
    sample = "é call +255712345678\nand +255700000000"
    assert [sample[m["start"] : m["end"]] for m in body["matches"]] == ["712345678", "700000000"]
    # Nothing is stored or audited.
    assert session.exec(select(RedactionRule).where(RedactionRule.detector.is_(None))).all() == []
    assert session.exec(select(AuditLog)).all() == []

    response = admin.post("/redaction-rules/test", json={"pattern": "x*", "sample": "a"})
    assert response.status_code == 422


def test_pattern_test_with_a_non_ascii_pattern(admin):
    body = admin.post(
        "/redaction-rules/test",
        json={"pattern": "NIF:(\\d{9})ç", "secret_group": 1, "sample": "é NIF:123456789ç"},
    ).json()
    assert body["redacted"] == "é NIF:[redacted: test]ç"
    assert body["matches"] == [{"start": 6, "end": 15}]


# --- system settings ------------------------------------------------------------


def test_redaction_settings_defaults(session, client_for):
    client = client_for(_user(session))
    body = client.get("/system-settings").json()
    assert body["redaction_enabled"] is False
    assert body["redaction_downloads"] is True
    assert body["redaction_download_suffix"] is True


@pytest.mark.parametrize(
    "key", ["redaction_enabled", "redaction_downloads", "redaction_download_suffix"]
)
def test_only_a_super_admin_changes_redaction_settings(session, client_for, key):
    manager = _user(session, global_capabilities=[GlobalCapability.manage_system_settings])
    client = client_for(manager)
    current = system_settings.get_bool(session, key)
    assert client.patch("/system-settings", json={key: not current}).status_code == 403
    assert system_settings.get_bool(session, key) == current
    # Other settings in the same request aren't saved either.
    response = client.patch("/system-settings", json={key: not current, "tail_interval_seconds": 5})
    assert response.status_code == 403
    assert system_settings.get_int(session, "tail_interval_seconds") == 2

    admin = client_for(_user(session, super_admin=True))
    response = admin.patch("/system-settings", json={key: not current})
    assert response.status_code == 200
    assert response.json()[key] is (not current)
    entry = _audit(session, "system_settings.update")[-1]
    assert entry.event_metadata[key] == {"old": current, "new": not current}
