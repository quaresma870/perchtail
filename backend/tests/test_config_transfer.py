"""Bulk import/export (#179)."""

import re
import textwrap

import pytest
import yaml
from app.api.auth import get_current_active_user
from app.api.config_transfer import router as config_router
from app.auth.models import AuditLog, Role, User
from app.crypto import decrypt_credential
from app.db import get_session
from app.models import (
    Customer,
    Folder,
    PatternKind,
    Protocol,
    Rule,
    RuleType,
    SearchIndexState,
    Source,
)
from app.timeutils import utcnow
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
            search_indexing_enabled: true
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


def test_changes_are_updates_that_clear_the_index_on_a_move(session, client):
    _import(client)
    web01 = session.exec(select(Source).where(Source.name == "web01")).one()
    session.add(
        SearchIndexState(source_id=web01.id, file_path="a.log", size=1, indexed_at=utcnow())
    )
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
    assert session.exec(select(SearchIndexState)).all() == []
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
