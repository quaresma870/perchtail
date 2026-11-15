"""Bulk import/export (#179)."""

import re
import textwrap

import pytest
import yaml
from app import config_transfer
from app.api.auth import get_current_active_user
from app.api.config_transfer import router as config_router
from app.auth.models import AuditLog, Role, User
from app.crypto import decrypt_credential
from app.db import get_session
from app.models import (
    AlertFileState,
    Customer,
    Folder,
    PatternKind,
    Protocol,
    Rule,
    RuleType,
    Source,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

TEMPLATE = textwrap.dedent(r"""
    version: 1
    customers:
      - name: Acme
        folders:
          - name: prod
            folders:
              - name: web
                sources:
                  - name: web01
                    protocol: ssh
                    host: web01.acme.example
                    base_path: /var/log/app
                    credential:
                      username: svc
                      password: ${env:PERCHTAIL_IMPORT_WEB01}
                    rules:
                      - include **/*.log
                      - exclude **/debug/**
                      - type: include
                        pattern: re:^access.*\.log$
                        notes: access logs
        sources:
          - name: iis01
            protocol: winrm
            host: iis01.acme.example
            roots:
              - {label: IIS, path: 'D:\Logs\IIS'}
              - {label: App, path: 'E:\App\logs'}
            rules: [include **/*.log]
          - name: fs01
            protocol: smb
            host: fs01.acme.example
            port: 4455
            base_path: Logs
    sources:
      - name: loose
        protocol: agent
        host: dmz-host
        base_path: /var/log
    """)


def _user(session, *, super_admin=True):
    role = Role(name=f"r-{super_admin}", is_super_admin=super_admin)
    session.add(role)
    session.commit()
    user = User(username=f"u-{super_admin}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setenv("PERCHTAIL_IMPORT_WEB01", "s3cret")
    user = _user(session)
    app = FastAPI()
    app.include_router(config_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _import(client, text=TEMPLATE, dry_run=False, **extra):
    response = client.post("/admin/config/import", json={"yaml": text, "dry_run": dry_run, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_dry_run_plans_without_writing(session, client):
    result = _import(client, dry_run=True)
    assert result["applied"] is False
    assert result["problems"] == []
    actions = {(i["kind"], i["key"]): i["action"] for i in result["items"]}
    assert actions[("customer", "Acme")] == "create"
    assert actions[("folder", "Acme / prod / web")] == "create"
    assert actions[("source", "Acme / prod / web / web01")] == "create"
    assert actions[("source", "(no customer) / loose")] == "create"
    needs = {i["key"] for i in result["items"] if i["needs_credential"]}
    assert needs == {"Acme / fs01", "Acme / iis01"}
    assert session.exec(select(Source)).all() == []
    assert session.exec(select(Customer)).all() == []


def _encrypted_private_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.BestAvailableEncryption(b"secret"),
        )
        .decode()
    )


ENCRYPTED_KEY_DETAIL = (
    "Encrypted SSH private keys aren't supported; provide the key without a passphrase"
)


def test_import_refuses_an_encrypted_private_key_given_with_the_request(session, client):
    response = client.post(
        "/admin/config/import",
        json={
            "yaml": TEMPLATE,
            "credentials": {
                "Acme / fs01": {"username": "fs", "private_key": _encrypted_private_key()}
            },
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == ENCRYPTED_KEY_DETAIL
    assert session.exec(select(Source)).all() == []


def test_import_refuses_an_encrypted_private_key_in_the_file(session, client, monkeypatch):
    monkeypatch.setenv("PERCHTAIL_IMPORT_KEY", _encrypted_private_key())
    text = textwrap.dedent("""
        version: 1
        customers:
          - name: Acme
            sources:
              - name: web02
                protocol: ssh
                host: web02.acme.example
                base_path: /var/log
                credential:
                  username: svc
                  private_key: ${env:PERCHTAIL_IMPORT_KEY}
        """)
    result = _import(client, text)
    assert result["applied"] is False
    assert any(p["message"] == ENCRYPTED_KEY_DETAIL for p in result["problems"])
    assert session.exec(select(Source)).all() == []


def test_apply_creates_everything_and_is_idempotent(session, client):
    result = _import(client, credentials={"Acme / fs01": {"username": "fs", "password": "pw"}})
    assert result["applied"] is True, result

    web01 = session.exec(select(Source).where(Source.name == "web01")).one()
    folder = session.get(Folder, web01.folder_id)
    assert folder.name == "web"
    assert session.get(Folder, folder.parent_folder_id).name == "prod"
    assert decrypt_credential(web01.credential_ref) == {"username": "svc", "password": "s3cret"}
    rules = session.exec(select(Rule).where(Rule.source_id == web01.id).order_by(Rule.order)).all()
    assert [(r.type, r.pattern, r.pattern_kind) for r in rules] == [
        (RuleType.include, "**/*.log", PatternKind.glob),
        (RuleType.exclude, "**/debug/**", PatternKind.glob),
        (RuleType.include, r"^access.*\.log$", PatternKind.regex),
    ]
    assert rules[2].notes == "access logs"
    iis = session.exec(select(Source).where(Source.name == "iis01")).one()
    assert [r["label"] for r in iis.roots] == ["IIS", "App"]
    fs = session.exec(select(Source).where(Source.name == "fs01")).one()
    assert decrypt_credential(fs.credential_ref)["username"] == "fs"
    assert session.exec(select(Source).where(Source.name == "loose")).one().customer_id is None

    again = _import(client)
    assert again["problems"] == []
    assert {i["action"] for i in again["items"]} == {"unchanged"}
    assert len(session.exec(select(Source)).all()) == 4
    assert session.exec(select(AuditLog).where(AuditLog.action == "config.import")).all()


def test_export_round_trips_without_secrets(session, client):
    _import(client)
    exported = client.get("/admin/config/export")
    assert exported.status_code == 200
    assert "s3cret" not in exported.text
    assert "credential" not in yaml.safe_load(exported.text)["customers"][0]["sources"][0]

    result = _import(client, text=exported.text)
    assert result["problems"] == []
    assert {i["action"] for i in result["items"]} == {"unchanged"}
    web01 = session.exec(select(Source).where(Source.name == "web01")).one()
    assert decrypt_credential(web01.credential_ref)["password"] == "s3cret"


def test_changes_are_updates_that_clear_alert_positions_on_a_move(session, client):
    _import(client)
    web01 = session.exec(select(Source).where(Source.name == "web01")).one()
    session.add(AlertFileState(alert_id=1, source_id=web01.id, file_path="a.log", offset=10))
    session.commit()

    changed = re.sub(
        r"\n *- exclude \*\*/debug/\*\*",
        "",
        TEMPLATE.replace("base_path: /var/log/app", "base_path: /srv/app"),
    )
    plan = _import(client, text=changed, dry_run=True)
    assert plan["problems"] == []
    item = next(i for i in plan["items"] if i["key"] == "Acme / prod / web / web01")
    assert item["action"] == "update"
    assert any("base_path" in c for c in item["changes"])
    assert any("rules replaced (3 → 2)" in c for c in item["changes"])

    _import(client, text=changed)
    session.refresh(web01)
    assert web01.base_path == "/srv/app"
    assert session.exec(select(AlertFileState)).all() == []
    assert len(session.exec(select(Rule).where(Rule.source_id == web01.id)).all()) == 2


def test_moving_a_source_to_another_folder_is_an_update(session, client):
    _import(client)
    moved = TEMPLATE.replace("- name: web\n", "- name: web-new\n")
    plan = _import(client, text=moved, dry_run=True)
    item = next(i for i in plan["items"] if i["key"] == "Acme / prod / web-new / web01")
    assert item["action"] == "update"
    assert "moved to another folder" in item["changes"]
    _import(client, text=moved)
    assert len(session.exec(select(Source).where(Source.name == "web01")).all()) == 1


@pytest.mark.parametrize(
    "mutate, where, fragment",
    [
        (lambda t: t.replace("version: 1", "version: 2"), "version", "version must be 1"),
        (lambda t: t.replace("protocol: ssh", "protocol: ftp"), "protocol", "protocol must be"),
        (
            lambda t: t.replace("base_path: Logs", r"base_path: 'C:\Logs'"),
            "base_path",
            "drive path",
        ),
        (lambda t: t.replace("- include **/*.log\n", "- include re:(a\n", 1), "rules[0]", "regex"),
        (lambda t: t.replace("- exclude **/debug/**", "- hide **/debug/**"), "rules[1]", "include"),
        (lambda t: t.replace("port: 4455", "prot: 4455"), "sources[1]", "Unknown keys: prot"),
        (
            lambda t: t.replace("${env:PERCHTAIL_IMPORT_WEB01}", "${env:PERCHTAIL_CREDENTIAL_KEY}"),
            "password",
            "Only ${env:PERCHTAIL_IMPORT_",
        ),
        (
            lambda t: t.replace("PERCHTAIL_IMPORT_WEB01", "PERCHTAIL_IMPORT_MISSING"),
            "password",
            "not set",
        ),
        (
            lambda t: t + "  - name: loose\n    protocol: agent\n    host: x\n    base_path: /\n",
            "sources[1]",
            "Same source",
        ),
        (lambda t: "{{{", "line", "Not valid YAML"),
        (
            lambda t: t.replace("- exclude **/debug/**", '- exclude "**/debug/**"'),
            "rules[1]",
            "in quotes",
        ),
        (
            lambda t: t.replace("- exclude **/debug/**", "- exclude '**/debug/**'"),
            "rules[1]",
            "exclude **/debug/** without them",
        ),
    ],
)
def test_problems_are_reported_with_where_and_nothing_is_written(
    session, client, mutate, where, fragment
):
    result = _import(client, text=mutate(TEMPLATE))
    assert result["applied"] is False
    assert result["problems"], result
    assert any(
        where in p["where"] and fragment in p["message"] for p in result["problems"]
    ), result["problems"]
    assert session.exec(select(Source)).all() == []


def test_protocol_cannot_change_on_reimport(session, client):
    _import(client)
    result = _import(
        client,
        text=TEMPLATE.replace("protocol: smb", "protocol: winrm").replace(
            "base_path: Logs", r"base_path: 'D:\Logs'"
        ),
    )
    assert result["applied"] is False
    assert any("protocol of a source can't be changed" in p["message"] for p in result["problems"])
    assert session.exec(select(Source).where(Source.name == "fs01")).one().protocol == Protocol.smb


def test_super_admin_only(session):
    user = _user(session, super_admin=False)
    app = FastAPI()
    app.include_router(config_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    c = TestClient(app)
    assert c.get("/admin/config/export").status_code == 403
    assert c.post("/admin/config/import", json={"yaml": TEMPLATE}).status_code == 403


def test_system_source_is_never_exported_or_matched(session, client):
    session.add(
        Source(
            name="loose", protocol=Protocol.local, host="localhost", base_path="/x", is_system=True
        )
    )
    session.commit()
    assert "is_system" not in client.get("/admin/config/export").text
    assert "/x" not in client.get("/admin/config/export").text
    result = _import(client)
    item = next(i for i in result["items"] if i["key"] == "(no customer) / loose")
    assert item["action"] == "create"


def test_the_documented_template_imports_cleanly(session, client, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("PERCHTAIL_IMPORT_WEB01_PASSWORD", "x")
    template = (
        Path(__file__).resolve().parents[2] / "docs/examples/perchtail-import.example.yaml"
    ).read_text()
    result = _import(client, text=template)
    assert result["problems"] == []
    assert result["applied"] is True


def test_import_respects_the_configured_folder_limit(session, client):
    from app import system_settings

    system_settings.set_int(session, "source_max_roots", 1)
    session.commit()
    result = _import(client, dry_run=True)
    assert result["applied"] is False
    assert any(
        p["where"].endswith("sources[0].roots") and "turned off" in p["message"]
        for p in result["problems"]
    ), result["problems"]


def test_import_cannot_repoint_a_source_and_keep_its_credential(session, client):
    _import(client)
    moved = TEMPLATE.replace("host: fs01.acme.example", "host: attacker.example")
    _import(client, text=moved, credentials={"Acme / fs01": {"username": "fs", "password": "pw"}})
    fs = session.exec(select(Source).where(Source.name == "fs01")).one()
    assert fs.host == "attacker.example"  # typed credential: allowed

    again = TEMPLATE.replace("host: fs01.acme.example", "host: evil.example")
    result = _import(client, text=again)
    assert result["applied"] is False
    assert any("credential again" in p["message"] for p in result["problems"])
    session.refresh(fs)
    assert fs.host == "attacker.example"


def test_quoting_the_whole_rule_is_fine(session, client):
    """#190: only a quoted *pattern* is refused, not a quoted rule."""
    result = _import(
        client, text=TEMPLATE.replace("- exclude **/debug/**", '- "exclude **/debug/**"')
    )
    assert result["problems"] == []
    assert "**/debug/**" in {r.pattern for r in session.exec(select(Rule)).all()}


def test_an_imported_source_without_a_credential_still_asks_for_one(session, client):
    """#196: after applying, a second preview showed the sources as
    unchanged and stopped offering the credential they still lack."""
    _import(client)
    again = _import(client, dry_run=True)
    needs = {i["key"]: i for i in again["items"] if i["needs_credential"]}
    assert set(needs) == {"Acme / fs01", "Acme / iis01"}
    assert needs["Acme / fs01"]["action"] == "unchanged"

    result = _import(
        client,
        credentials={"Acme / fs01": {"username": "svc", "password": "pw"}},
    )
    item = next(i for i in result["items"] if i["key"] == "Acme / fs01")
    assert item["action"] == "update" and "credential set" in item["changes"]
    assert item["needs_credential"] is False
    fs01 = session.exec(select(Source).where(Source.name == "fs01")).one()
    assert fs01.credential_ref is not None


def test_a_port_change_without_a_credential_points_at_the_port(session, client):
    """#206: the problem pointed at `.host` when only the port changed."""
    _import(client, credentials={"Acme / fs01": {"username": "fs", "password": "pw"}})
    result = _import(client, text=TEMPLATE.replace("port: 4455", "port: 4456"))
    assert result["applied"] is False
    wheres = [p["where"] for p in result["problems"] if "credential again" in p["message"]]
    assert len(wheres) == 1 and wheres[0].endswith(".port")


def test_name_index_flag_round_trips(session):
    yaml_text = """
version: 1
customers:
  - name: Acme
    sources:
      - name: quiet
        protocol: ssh
        host: h1
        base_path: /var/log
        name_index_enabled: false
"""
    items, problems = config_transfer.run(
        session, config_transfer.parse(yaml_text), credentials={}, apply=True, user_id=None
    )
    assert problems == []
    session.commit()
    source = session.exec(select(Source).where(Source.name == "quiet")).one()
    assert source.name_index_enabled is False
    assert "name_index_enabled: false" in config_transfer.export(session)


def test_an_agents_upgrade_policy_round_trips(session):
    yaml_text = """
version: 1
sources:
  - name: dmz
    protocol: agent
    host: dmz-host
    base_path: /var/log
    agent_upgrade_policy: automatic
  - name: follows
    protocol: agent
    host: other
    base_path: /var/log
"""
    items, problems = config_transfer.run(
        session, config_transfer.parse(yaml_text), credentials={}, apply=True, user_id=None
    )
    assert problems == []
    session.commit()
    dmz = session.exec(select(Source).where(Source.name == "dmz")).one()
    assert dmz.agent_upgrade_policy == "automatic"
    follows = session.exec(select(Source).where(Source.name == "follows")).one()
    assert follows.agent_upgrade_policy == "inherit"
    exported = config_transfer.export(session)
    assert exported.count("agent_upgrade_policy") == 1
    assert "agent_upgrade_policy: automatic" in exported

    # Changing it is an update.
    items, problems = config_transfer.run(
        session,
        config_transfer.parse(yaml_text.replace("policy: automatic", "policy: off")),
        credentials={},
        apply=False,
        user_id=None,
    )
    assert problems == []
    [update] = [i for i in items if i.action == "update"]
    assert "agent_upgrade_policy" in update.changes[0]


@pytest.mark.parametrize(
    ("protocol", "value", "fragment"),
    [
        ("agent", "always", "must be one of inherit, off, manual, automatic"),
        ("ssh", "manual", "push-agent sources only"),
    ],
)
def test_a_bad_upgrade_policy_is_a_problem(session, protocol, value, fragment):
    yaml_text = f"""
version: 1
sources:
  - name: one
    protocol: {protocol}
    host: h1
    base_path: /var/log
    agent_upgrade_policy: {value}
"""
    parsed = config_transfer.parse(yaml_text)
    assert any(
        p.where.endswith("agent_upgrade_policy") and fragment in p.message for p in parsed.problems
    ), parsed.problems


# --- redaction (#382) ---------------------------------------------------------

REDACTION_YAML = r"""
version: 1
sources:
  - name: red
    protocol: ssh
    host: red-host
    base_path: /var/log
    rules: [include **/*.log]
    redaction_rules:
      - name: msisdn
        pattern: \+255(\d{9})
        secret_group: 1
        max_length: 13
      - name: ref
        pattern: REF-\d{6}
        max_length: 10
        enabled: false
        notes: order references
    redaction_disable: [ipv4, Email]
"""


def _run(session, text, apply=True):
    items, problems = config_transfer.run(
        session, config_transfer.parse(text), credentials={}, apply=apply, user_id=None
    )
    if apply and not problems:
        session.commit()
    return items, problems


def test_redaction_rules_and_disabled_system_rules_import_and_round_trip(session):
    from app.models import RedactionRule
    from app.redaction_rules import (
        disabled_rule_ids,
        seed_builtin_redaction_rules,
        source_rules,
        system_rules,
    )

    seed_builtin_redaction_rules(session)
    items, problems = _run(session, REDACTION_YAML)
    assert problems == []
    [item] = items
    assert "2 redaction rules" in item.changes
    source = session.exec(select(Source).where(Source.name == "red")).one()
    own = source_rules(session, source.id)
    assert [(r.name, r.pattern, r.secret_group, r.max_length, r.enabled, r.notes) for r in own] == [
        ("msisdn", r"\+255(\d{9})", 1, 13, True, None),
        ("ref", r"REF-\d{6}", None, 10, False, "order references"),
    ]
    by_id = {r.id: r.name for r in system_rules(session)}
    assert sorted(by_id[i] for i in disabled_rule_ids(session, source.id)) == ["email", "ipv4"]
    created = session.exec(select(AuditLog).where(AuditLog.action == "source.create")).one()
    # The audit keeps each rule's values, as the API's redaction_rule.create does.
    assert created.event_metadata["redaction_rules"] == [
        {
            "name": "msisdn",
            "pattern": r"\+255(\d{9})",
            "secret_group": 1,
            "max_length": 13,
            "enabled": True,
            "order": 0,
            "notes": None,
        },
        {
            "name": "ref",
            "pattern": r"REF-\d{6}",
            "secret_group": None,
            "max_length": 10,
            "enabled": False,
            "order": 1,
            "notes": "order references",
        },
    ]
    assert created.event_metadata["redaction_disable"] == ["email", "ipv4"]

    exported = config_transfer.export(session)
    node = yaml.safe_load(exported)["sources"][0]
    assert node["redaction_disable"] == ["email", "ipv4"]
    assert node["redaction_rules"][0] == {
        "name": "msisdn",
        "pattern": r"\+255(\d{9})",
        "max_length": 13,
        "secret_group": 1,
    }
    assert node["redaction_rules"][1]["enabled"] is False
    # System rules aren't exported.
    assert "card" not in exported

    items, problems = _run(session, exported)
    assert problems == []
    assert [i.action for i in items] == ["unchanged"]
    assert len(session.exec(select(RedactionRule)).all()) == 11 + 2


def test_reimport_replaces_redaction_rules_and_audits_it(session):
    from app.redaction_rules import (
        disabled_rule_ids,
        seed_builtin_redaction_rules,
        source_rules,
    )

    seed_builtin_redaction_rules(session)
    _run(session, REDACTION_YAML)
    changed = REDACTION_YAML.replace("max_length: 10", "max_length: 12").replace(
        "redaction_disable: [ipv4, Email]", "redaction_disable: []"
    )
    items, problems = _run(session, changed)
    assert problems == []
    [item] = items
    assert item.action == "update"
    assert "redaction rules replaced (2 → 2)" in item.changes
    assert "redaction rules turned off: none" in item.changes
    source = session.exec(select(Source).where(Source.name == "red")).one()
    assert [r.max_length for r in source_rules(session, source.id)] == [13, 12]
    assert disabled_rule_ids(session, source.id) == set()
    update = session.exec(select(AuditLog).where(AuditLog.action == "source.update")).one()
    changes = update.event_metadata["changes"]
    # Only the rule that changed is recorded, with its values before and after.
    [ref_change] = changes["redaction_rules"]
    assert ref_change["name"] == "ref"
    assert ref_change["from"]["max_length"] == 10
    assert ref_change["to"]["max_length"] == 12
    assert changes["redaction_disable"] == {"from": ["email", "ipv4"], "to": []}

    # Leaving the keys out keeps what the source has.
    kept = "\n".join(
        line
        for line in REDACTION_YAML.splitlines()
        if not line.startswith("    redaction") and not line.startswith("      ")
    )
    items, problems = _run(session, kept)
    assert problems == []
    assert [i.action for i in items] == ["unchanged"]
    assert len(source_rules(session, source.id)) == 2


def test_reimport_audits_a_rule_turned_off_a_new_pattern_and_added_and_removed_rules(session):
    from app.redaction_rules import seed_builtin_redaction_rules

    seed_builtin_redaction_rules(session)
    _run(session, REDACTION_YAML)
    # msisdn is turned off and gets a pattern that never matches; ref is
    # removed and acct added. A name-only audit would hide the first change.
    changed = REDACTION_YAML.replace(
        "        pattern: \\+255(\\d{9})\n        secret_group: 1\n        max_length: 13\n",
        "        pattern: \\+999(\\d{9})\n        secret_group: 1\n        max_length: 13\n"
        "        enabled: false\n",
    ).replace(
        "      - name: ref\n        pattern: REF-\\d{6}\n        max_length: 10\n"
        "        enabled: false\n        notes: order references\n",
        "      - name: acct\n        pattern: ACCT-\\d{4}\n        max_length: 9\n",
    )
    assert "+999" in changed and "acct" in changed and "REF-" not in changed
    items, problems = _run(session, changed)
    assert problems == []
    update = session.exec(select(AuditLog).where(AuditLog.action == "source.update")).one()
    by_name = {c["name"]: c for c in update.event_metadata["changes"]["redaction_rules"]}
    assert set(by_name) == {"msisdn", "ref", "acct"}
    msisdn = by_name["msisdn"]
    assert (msisdn["from"]["pattern"], msisdn["from"]["enabled"]) == (r"\+255(\d{9})", True)
    assert (msisdn["to"]["pattern"], msisdn["to"]["enabled"]) == (r"\+999(\d{9})", False)
    assert by_name["ref"]["to"] is None
    assert by_name["ref"]["from"]["pattern"] == r"REF-\d{6}"
    assert by_name["acct"]["from"] is None
    assert by_name["acct"]["to"] == {
        "name": "acct",
        "pattern": r"ACCT-\d{4}",
        "secret_group": None,
        "max_length": 9,
        "enabled": True,
        "order": 1,
        "notes": None,
    }


@pytest.mark.parametrize(
    ("snippet", "fragment"),
    [
        ("redaction_rules: [{name: x, pattern: 'x*', max_length: 5}]", "matches empty text"),
        ("redaction_rules: [{name: x, pattern: 'ab{20}', max_length: 5}]", "up to 21 bytes"),
        ("redaction_rules: [{name: x, pattern: 'ab'}]", "max_length"),
        ("redaction_rules: [{name: x, pattern: 'ab', max_length: 5, color: red}]", "Unknown keys"),
        (
            "redaction_rules: [{name: x, pattern: a, max_length: 1}, "
            "{name: X, pattern: b, max_length: 1}]",
            "listed twice",
        ),
        ("redaction_rules: x", "must be a list"),
        ("redaction_disable: [1]", "list of system rule names"),
    ],
)
def test_bad_redaction_keys_are_problems(session, snippet, fragment):
    text = f"""
version: 1
sources:
  - name: one
    protocol: ssh
    host: h1
    base_path: /var/log
    {snippet}
"""
    parsed = config_transfer.parse(text)
    assert any(fragment in p.message for p in parsed.problems), parsed.problems


def test_an_unknown_system_rule_name_is_a_problem_and_nothing_is_written(session):
    from app.redaction_rules import seed_builtin_redaction_rules

    seed_builtin_redaction_rules(session)
    text = REDACTION_YAML.replace("[ipv4, Email]", "[ipv4, nope]")
    items, problems = _run(session, text)
    assert [p.where for p in problems] == ["sources[0].redaction_disable"]
    assert "'nope'" in problems[0].message
