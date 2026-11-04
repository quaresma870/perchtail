"""Bulk import and export of customers, folders, sources and rules as one
YAML file (#179). See docs/bulk-import.md for the format.

Import is declarative and idempotent: running the same file twice changes
nothing the second time. Things are matched by name -- a customer by its
name, a folder by its name under the same parent, a source by its name
under the same customer -- and anything in the database that the file
doesn't mention is left alone (nothing is ever deleted by an import).

A source's `rules`, when given, replace its rule list; leave the key out to
keep the rules it has. Credentials are never exported. On import a source
gets a credential from the file (inline, or `${env:PERCHTAIL_IMPORT_...}`
read from the server's environment), from the values typed in the import
screen, or keeps the one it already has.

The whole file is checked before anything is written, and applying it is
one transaction: either every change lands or none does."""

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from fastapi import HTTPException
from sqlmodel import Session, select

from app import source_roots
from app.audit import record_audit_event
from app.crypto import decrypt_credential, encrypt_credential
from app.logging_config import get_logger
from app.models import Customer, Folder, PatternKind, Protocol, Rule, RuleType, Source
from app.rules import InvalidPatternError, parse_pattern
from app.search_index import delete_source_index

logger = get_logger(__name__)

SCHEMA_VERSION = 1
MAX_BYTES = 1_000_000
MAX_SOURCES = 5000
_ENV_REF = re.compile(r"^\$\{env:(PERCHTAIL_IMPORT_[A-Za-z0-9_]+)\}$")
_CREDENTIAL_PROTOCOLS = (Protocol.ssh, Protocol.smb, Protocol.winrm)
_SOURCE_KEYS = {
    "name",
    "protocol",
    "host",
    "port",
    "base_path",
    "roots",
    "enabled",
    "search_indexing_enabled",
    "credential",
    "rules",
    "winrm_scheme",
    "tls_ca_pem",
}
_CONTAINER_KEYS = {"name", "folders", "sources"}


@dataclass
class Problem:
    where: str
    message: str


@dataclass
class PlanItem:
    action: str  # create | update | unchanged
    kind: str  # customer | folder | source
    key: str
    changes: list[str] = field(default_factory=list)
    needs_credential: bool = False


@dataclass
class _RuleSpec:
    type: RuleType
    pattern: str
    pattern_kind: PatternKind
    notes: str | None


@dataclass
class _SourceSpec:
    where: str
    key: str
    customer: str | None
    folders: tuple[str, ...]
    name: str
    protocol: Protocol
    host: str
    port: int | None
    base_path: str
    roots: list[dict] | None
    enabled: bool
    search_indexing_enabled: bool
    credential: dict | None
    rules: list[_RuleSpec] | None
    winrm_scheme: str | None = None
    tls_ca_pem: str | None = None


@dataclass
class Parsed:
    customers: list[str] = field(default_factory=list)
    folders: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    sources: list[_SourceSpec] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def source_key(customer: str | None, folders: tuple[str, ...], name: str) -> str:
    return " / ".join([customer or "(no customer)", *folders, name])


# --- parsing ---------------------------------------------------------------


def parse(text: str) -> Parsed:
    parsed = Parsed()
    if len(text.encode("utf-8")) > MAX_BYTES:
        parsed.problems.append(Problem("file", f"The file is larger than {MAX_BYTES} bytes"))
        return parsed
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark is not None else "file"
        parsed.problems.append(Problem(where, f"Not valid YAML: {getattr(exc, 'problem', exc)}"))
        return parsed
    if not isinstance(doc, dict):
        parsed.problems.append(Problem("file", "Expected a mapping with version and customers"))
        return parsed
    unknown = set(doc) - {"version", "customers", "sources"}
    if unknown:
        parsed.problems.append(
            Problem("file", f"Unknown keys: {', '.join(sorted(map(str, unknown)))}")
        )
    if doc.get("version") != SCHEMA_VERSION:
        parsed.problems.append(Problem("version", f"version must be {SCHEMA_VERSION}"))

    seen_customers: set[str] = set()
    for i, node in enumerate(_list(doc.get("customers"), "customers", parsed)):
        where = f"customers[{i}]"
        name = _container_name(node, where, parsed)
        if name is None:
            continue
        if name in seen_customers:
            parsed.problems.append(Problem(where, f"Customer {name!r} is listed twice"))
            continue
        seen_customers.add(name)
        parsed.customers.append(name)
        _walk(node, name, (), where, parsed)

    for j, node in enumerate(_list(doc.get("sources"), "sources", parsed)):
        _parse_source(node, None, (), f"sources[{j}]", parsed)

    keys: dict[str, str] = {}
    for spec in parsed.sources:
        if spec.key in keys:
            parsed.problems.append(
                Problem(spec.where, f"Same source as {keys[spec.key]} ({spec.key})")
            )
        keys.setdefault(spec.key, spec.where)
    if len(parsed.sources) > MAX_SOURCES:
        parsed.problems.append(Problem("file", f"At most {MAX_SOURCES} sources per import"))
    return parsed


def _list(value: Any, where: str, parsed: Parsed) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        parsed.problems.append(Problem(where, "Expected a list"))
        return []
    return value


def _container_name(node: Any, where: str, parsed: Parsed) -> str | None:
    if not isinstance(node, dict):
        parsed.problems.append(Problem(where, "Expected a mapping with a name"))
        return None
    unknown = set(node) - _CONTAINER_KEYS
    if unknown:
        parsed.problems.append(
            Problem(where, f"Unknown keys: {', '.join(sorted(map(str, unknown)))}")
        )
    name = node.get("name")
    if not isinstance(name, str) or not name.strip():
        parsed.problems.append(Problem(where, "name is required"))
        return None
    return name.strip()


def _walk(node: dict, customer: str, path: tuple[str, ...], where: str, parsed: Parsed) -> None:
    for j, source in enumerate(_list(node.get("sources"), f"{where}.sources", parsed)):
        _parse_source(source, customer, path, f"{where}.sources[{j}]", parsed)
    seen: set[str] = set()
    for k, folder in enumerate(_list(node.get("folders"), f"{where}.folders", parsed)):
        folder_where = f"{where}.folders[{k}]"
        name = _container_name(folder, folder_where, parsed)
        if name is None:
            continue
        if name in seen:
            parsed.problems.append(Problem(folder_where, f"Folder {name!r} is listed twice here"))
            continue
        seen.add(name)
        parsed.folders.append((customer, path + (name,)))
        _walk(folder, customer, path + (name,), folder_where, parsed)


def _parse_source(
    node: Any, customer: str | None, folders: tuple[str, ...], where: str, parsed: Parsed
) -> None:
    from app.api.sources import (
        SourceRootModel,
        _validate_base_path,
        _validate_ca_pem,
        _validate_roots,
        _validate_winrm_scheme,
    )

    problems = parsed.problems
    if not isinstance(node, dict):
        problems.append(Problem(where, "Expected a mapping"))
        return
    unknown = set(node) - _SOURCE_KEYS
    if unknown:
        problems.append(Problem(where, f"Unknown keys: {', '.join(sorted(map(str, unknown)))}"))
    name = node.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(Problem(where, "name is required"))
        return
    name = name.strip()
    try:
        protocol = Protocol(node.get("protocol"))
    except ValueError:
        problems.append(
            Problem(
                f"{where}.protocol",
                f"protocol must be one of {', '.join(p.value for p in Protocol)}",
            )
        )
        return
    host = node.get("host")
    if not isinstance(host, str) or not host.strip():
        problems.append(Problem(f"{where}.host", "host is required"))
        return
    port = node.get("port")
    if port is not None and (
        not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536
    ):
        problems.append(Problem(f"{where}.port", "port must be a number from 1 to 65535"))
        return
    flags = {}
    for flag, default in (("enabled", True), ("search_indexing_enabled", False)):
        value = node.get(flag, default)
        if not isinstance(value, bool):
            problems.append(Problem(f"{where}.{flag}", f"{flag} must be true or false"))
            return
        flags[flag] = value

    roots = None
    try:
        if node.get("roots") is not None:
            raw_roots = node["roots"]
            if not isinstance(raw_roots, list) or not all(isinstance(r, dict) for r in raw_roots):
                problems.append(Problem(f"{where}.roots", "roots must be a list of label/path"))
                return
            models = [
                SourceRootModel(label=str(r.get("label", "")), path=str(r.get("path", "")))
                for r in raw_roots
            ]
            roots = _validate_roots(protocol, models)
            if roots:
                base_path = roots[0]["path"]
            elif models:
                base_path = _validate_base_path(protocol, models[0].path)
            else:
                problems.append(Problem(f"{where}.roots", "roots is empty"))
                return
        else:
            raw_path = node.get("base_path")
            if not isinstance(raw_path, str):
                problems.append(Problem(f"{where}.base_path", "base_path (or roots) is required"))
                return
            base_path = (
                raw_path if protocol == Protocol.agent else _validate_base_path(protocol, raw_path)
            )
    except HTTPException as exc:
        problems.append(Problem(f"{where}.base_path", str(exc.detail)))
        return

    scheme = node.get("winrm_scheme")
    if scheme is not None and scheme not in ("https", "http"):
        problems.append(Problem(f"{where}.winrm_scheme", "winrm_scheme must be https or http"))
        return
    raw_ca = node.get("tls_ca_pem")
    if raw_ca is not None and not isinstance(raw_ca, str):
        problems.append(Problem(f"{where}.tls_ca_pem", "tls_ca_pem must be PEM text"))
        return
    try:
        scheme = _validate_winrm_scheme(protocol, scheme)
        ca_pem = _validate_ca_pem(protocol, raw_ca)
    except HTTPException as exc:
        problems.append(Problem(f"{where}.winrm_scheme", str(exc.detail)))
        return
    if protocol == Protocol.winrm and scheme is None:
        # As in the editor: a WinRM source always stores its scheme.
        scheme = "https" if not port or port == 5986 else "http"

    credential = _parse_credential(
        node.get("credential"), protocol, f"{where}.credential", problems
    )
    rules = _parse_rules(node.get("rules"), f"{where}.rules", problems)

    parsed.sources.append(
        _SourceSpec(
            where=where,
            key=source_key(customer, folders, name),
            customer=customer,
            folders=folders,
            name=name,
            protocol=protocol,
            host=host.strip(),
            port=port,
            base_path=base_path,
            roots=roots,
            credential=credential,
            rules=rules,
            winrm_scheme=scheme,
            tls_ca_pem=ca_pem,
            **flags,
        )
    )


def _parse_credential(
    value: Any, protocol: Protocol, where: str, problems: list[Problem]
) -> dict | None:
    if value is None:
        return None
    if protocol not in _CREDENTIAL_PROTOCOLS:
        problems.append(Problem(where, f"{protocol.value} sources take no credential"))
        return None
    if not isinstance(value, dict) or set(value) - {"username", "password", "private_key"}:
        problems.append(Problem(where, "credential takes username plus password or private_key"))
        return None
    resolved = {}
    for key, raw in value.items():
        if not isinstance(raw, str):
            problems.append(Problem(f"{where}.{key}", "must be a string"))
            return None
        match = _ENV_REF.match(raw.strip())
        if match:
            env_value = os.environ.get(match.group(1))
            if env_value is None:
                problems.append(
                    Problem(f"{where}.{key}", f"{match.group(1)} is not set on the server")
                )
                return None
            raw = env_value
        elif raw.strip().startswith("${"):
            problems.append(
                Problem(
                    f"{where}.{key}",
                    "Only ${env:PERCHTAIL_IMPORT_...} references are read from the environment",
                )
            )
            return None
        resolved[key] = raw
    if not resolved.get("username") or not (
        resolved.get("password") or resolved.get("private_key")
    ):
        problems.append(Problem(where, "credential needs a username and a password or private_key"))
        return None
    return resolved


def _quoted(raw: str) -> bool:
    return len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'"


def _parse_rules(value: Any, where: str, problems: list[Problem]) -> list[_RuleSpec] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        problems.append(Problem(where, "rules must be a list"))
        return None
    out = []
    for i, item in enumerate(value):
        rule_where = f"{where}[{i}]"
        notes = None
        if isinstance(item, str):
            type_word, _, raw = item.strip().partition(" ")
        elif isinstance(item, dict) and not set(item) - {"type", "pattern", "notes"}:
            type_word, raw, notes = (
                str(item.get("type", "")),
                str(item.get("pattern", "")),
                item.get("notes"),
            )
            if notes is not None and not isinstance(notes, str):
                problems.append(Problem(rule_where, "notes must be text"))
                continue
        else:
            problems.append(
                Problem(rule_where, 'Expected "include <pattern>" or "exclude <pattern>"')
            )
            continue
        try:
            rule_type = RuleType(type_word)
        except ValueError:
            problems.append(Problem(rule_where, 'A rule starts with "include" or "exclude"'))
            continue
        raw = raw.strip()
        if not raw:
            problems.append(Problem(rule_where, "The rule has no pattern"))
            continue
        if isinstance(item, str) and _quoted(raw):
            # `[include **/*, exclude "*/secret/**"]` is valid YAML, but each
            # item is one plain string, so the quotes would be part of the
            # pattern and the rule would never match (#190).
            problems.append(
                Problem(
                    rule_where,
                    f"The pattern is in quotes, which would become part of it: write "
                    f"{rule_type.value} {raw[1:-1]} without them, or quote the whole rule",
                )
            )
            continue
        try:
            pattern, kind = parse_pattern(raw)
        except InvalidPatternError as exc:
            problems.append(Problem(rule_where, str(exc)))
            continue
        out.append(_RuleSpec(rule_type, pattern, kind, notes))
    return out


# --- planning and applying -------------------------------------------------


def _rules_tuple(rules) -> list[tuple]:
    return [(r.type, r.pattern, r.pattern_kind, r.notes or None) for r in rules]


def run(
    session: Session,
    parsed: Parsed,
    *,
    credentials: dict[str, dict],
    apply: bool,
    user_id: int | None,
) -> tuple[list[PlanItem], list[Problem]]:
    """Works out what the file changes and, with apply=True, makes those
    changes (without committing -- the caller commits or rolls back)."""
    items: list[PlanItem] = []
    problems = list(parsed.problems)
    if problems:
        return items, problems

    roots_limit = source_roots.max_roots(session)
    customer_ids: dict[str, int | None] = {}
    for name in parsed.customers:
        customer = session.exec(select(Customer).where(Customer.name == name)).first()
        if customer is not None:
            customer_ids[name] = customer.id
            items.append(PlanItem("unchanged", "customer", name))
            continue
        items.append(PlanItem("create", "customer", name))
        customer_ids[name] = None
        if apply:
            customer = Customer(name=name)
            session.add(customer)
            session.flush()
            customer_ids[name] = customer.id
            record_audit_event(
                session,
                user_id=user_id,
                action="customer.create",
                target_type="customer",
                target_id=customer.id,
                metadata={"name": name, "via": "import"},
            )

    folder_ids: dict[tuple[str, tuple[str, ...]], int | None] = {}
    for customer, path in parsed.folders:
        parent_id = folder_ids.get((customer, path[:-1])) if len(path) > 1 else None
        customer_id = customer_ids[customer]
        existing = None
        if customer_id is not None and (len(path) == 1 or parent_id is not None):
            existing = session.exec(
                select(Folder).where(
                    Folder.customer_id == customer_id,
                    Folder.parent_folder_id == parent_id,
                    Folder.name == path[-1],
                )
            ).first()
        key = " / ".join((customer, *path))
        if existing is not None:
            folder_ids[(customer, path)] = existing.id
            items.append(PlanItem("unchanged", "folder", key))
            continue
        items.append(PlanItem("create", "folder", key))
        folder_ids[(customer, path)] = None
        if apply:
            folder = Folder(name=path[-1], customer_id=customer_id, parent_folder_id=parent_id)
            session.add(folder)
            session.flush()
            folder_ids[(customer, path)] = folder.id
            record_audit_event(
                session,
                user_id=user_id,
                action="folder.create",
                target_type="folder",
                target_id=folder.id,
                metadata={"name": folder.name, "via": "import"},
            )

    for spec in parsed.sources:
        customer_id = customer_ids.get(spec.customer) if spec.customer else None
        folder_id = folder_ids.get((spec.customer, spec.folders)) if spec.folders else None
        # Sources are matched by name under their customer, wherever they
        # sit in its folders, so moving one between folders is an update.
        new_parent = spec.customer is not None and customer_id is None
        existing: list[Source] = []
        if not new_parent:
            existing = list(
                session.exec(
                    select(Source).where(
                        Source.customer_id == customer_id,
                        Source.name == spec.name,
                        Source.is_system == False,  # noqa: E712 - SQL expression
                    )
                ).all()
            )
        if len(existing) > 1:
            problems.append(
                Problem(spec.where, f"{len(existing)} sources are already named {spec.name!r} here")
            )
            continue
        # Same rule as the editor: the configured limit, or the folders an
        # existing source already has if that is more.
        allowed = max(roots_limit, len(source_roots.roots_of(existing[0])) if existing else 0)
        if spec.roots and len(spec.roots) > allowed:
            problems.append(
                Problem(f"{spec.where}.roots", source_roots.roots_limit_message(allowed))
            )
            continue
        credential = credentials.get(spec.key) or spec.credential
        if existing:
            item = _update_source(session, existing[0], spec, folder_id, credential, apply, user_id)
        else:
            item = _create_source(session, spec, customer_id, folder_id, credential, apply, user_id)
        if isinstance(item, Problem):
            problems.append(item)
        else:
            items.append(item)
    return items, problems


def _create_source(session, spec, customer_id, folder_id, credential, apply, user_id):
    item = PlanItem(
        "create",
        "source",
        spec.key,
        changes=[f"{spec.protocol.value}://{spec.host}{':' + str(spec.port) if spec.port else ''}"]
        + ([f"{len(spec.rules)} rules"] if spec.rules else ["no rules (nothing visible)"]),
        needs_credential=spec.protocol in _CREDENTIAL_PROTOCOLS and credential is None,
    )
    if spec.protocol == Protocol.agent:
        item.changes.append("generate the agent token in the source's settings afterwards")
    if not apply:
        return item
    source = Source(
        name=spec.name,
        customer_id=customer_id,
        folder_id=folder_id,
        protocol=spec.protocol,
        host=spec.host,
        port=spec.port,
        base_path=spec.base_path,
        roots=spec.roots,
        enabled=spec.enabled,
        search_indexing_enabled=spec.search_indexing_enabled,
        credential_ref=encrypt_credential(credential) if credential else None,
        winrm_scheme=spec.winrm_scheme,
        tls_ca_pem=spec.tls_ca_pem,
    )
    session.add(source)
    session.flush()
    _replace_rules(session, source, spec.rules or [])
    record_audit_event(
        session,
        user_id=user_id,
        action="source.create",
        target_type="source",
        target_id=source.id,
        metadata={
            "name": source.name,
            "protocol": source.protocol,
            "host": source.host,
            "base_path": source.base_path,
            "via": "import",
        },
    )
    return item


def _update_source(session, source, spec, folder_id, credential, apply, user_id):
    if source.protocol != spec.protocol:
        return Problem(
            spec.where,
            f"{spec.name!r} already exists as a {source.protocol.value} source; "
            "the protocol of a source can't be changed",
        )
    if (
        (spec.host, spec.port) != (source.host, source.port)
        and source.credential_ref is not None
        and credential is None
    ):
        from app.api.sources import CREDENTIAL_REQUIRED_FOR_NEW_HOST

        return Problem(f"{spec.where}.host", CREDENTIAL_REQUIRED_FOR_NEW_HOST)
    wanted = {
        "folder_id": folder_id,
        "host": spec.host,
        "port": spec.port,
        "base_path": spec.base_path,
        "roots": spec.roots,
        "enabled": spec.enabled,
        "search_indexing_enabled": spec.search_indexing_enabled,
        "winrm_scheme": spec.winrm_scheme,
        "tls_ca_pem": spec.tls_ca_pem,
    }
    changes = {
        k: {"from": getattr(source, k), "to": v}
        for k, v in wanted.items()
        if getattr(source, k) != v
    }
    current_rules = sorted(source.rules, key=lambda r: r.order)
    rules_change = spec.rules is not None and _rules_tuple(current_rules) != _rules_tuple(
        spec.rules
    )
    credential_change = credential is not None and (
        source.credential_ref is None or decrypt_credential(source.credential_ref) != credential
    )
    labels = [
        f"{k}: {v['from']!r} → {v['to']!r}"
        for k, v in changes.items()
        if k not in ("folder_id", "tls_ca_pem")
    ]
    if "tls_ca_pem" in changes:
        labels.append("CA certificate " + ("replaced" if spec.tls_ca_pem else "removed"))
    if "folder_id" in changes:
        labels.append("moved to another folder")
    if rules_change:
        labels.append(f"rules replaced ({len(current_rules)} → {len(spec.rules)})")
    if credential_change:
        labels.append("credential replaced")
    if not labels:
        return PlanItem("unchanged", "source", spec.key)
    item = PlanItem("update", "source", spec.key, changes=labels)
    if not apply:
        return item

    location_changed = any(k in changes for k in ("host", "port", "base_path", "roots"))
    for key, change in changes.items():
        setattr(source, key, change["to"])
    if credential_change:
        source.credential_ref = encrypt_credential(credential)
    if rules_change:
        _replace_rules(session, source, spec.rules)
    if location_changed or not (source.enabled and source.search_indexing_enabled):
        delete_source_index(session, source.id)
    session.add(source)
    audit_changes = {k: v for k, v in changes.items()}
    if "tls_ca_pem" in audit_changes:
        audit_changes["tls_ca_pem"] = "replaced" if spec.tls_ca_pem else "removed"
    if rules_change:
        audit_changes["rules"] = "replaced"
    if credential_change:
        audit_changes["credential"] = "replaced"
    record_audit_event(
        session,
        user_id=user_id,
        action="source.update",
        target_type="source",
        target_id=source.id,
        metadata={"changes": audit_changes, "via": "import"},
    )
    return item


def _replace_rules(session: Session, source: Source, rules: list[_RuleSpec]) -> None:
    for rule in session.exec(select(Rule).where(Rule.source_id == source.id)).all():
        session.delete(rule)
    session.flush()
    for order, spec in enumerate(rules):
        session.add(
            Rule(
                source_id=source.id,
                order=order,
                type=spec.type,
                pattern=spec.pattern,
                pattern_kind=spec.pattern_kind,
                notes=spec.notes,
            )
        )
    session.flush()
    session.refresh(source)


# --- export ----------------------------------------------------------------

_EXPORT_HEADER = """\
# PerchTail configuration export (see docs/bulk-import.md).
# Credentials are never exported: re-importing this file keeps the
# credential each existing source already has. For new sources, add a
# credential block or type it in the import screen.
"""


def _rule_out(rule: Rule) -> str | dict:
    pattern = f"re:{rule.pattern}" if rule.pattern_kind == PatternKind.regex else rule.pattern
    if rule.notes:
        return {"type": rule.type.value, "pattern": pattern, "notes": rule.notes}
    return f"{rule.type.value} {pattern}"


def _source_out(source: Source) -> dict:
    out: dict[str, Any] = {
        "name": source.name,
        "protocol": source.protocol.value,
        "host": source.host,
    }
    if source.port:
        out["port"] = source.port
    if source_roots.is_multi_root(source):
        out["roots"] = [{"label": r["label"], "path": r["path"]} for r in source.roots]
    else:
        out["base_path"] = source.base_path
    if source.protocol == Protocol.winrm and source.winrm_scheme:
        out["winrm_scheme"] = source.winrm_scheme
    if source.tls_ca_pem:
        out["tls_ca_pem"] = source.tls_ca_pem
    if not source.enabled:
        out["enabled"] = False
    if source.search_indexing_enabled:
        out["search_indexing_enabled"] = True
    out["rules"] = [_rule_out(r) for r in sorted(source.rules, key=lambda r: r.order)]
    return out


def export(session: Session) -> str:
    sources = session.exec(select(Source).where(Source.is_system == False)).all()  # noqa: E712
    folders = session.exec(select(Folder)).all()
    by_folder: dict[int | None, list[Source]] = {}
    for source in sources:
        by_folder.setdefault(source.folder_id, []).append(source)
    children: dict[int | None, list[Folder]] = {}
    for folder in folders:
        children.setdefault(folder.parent_folder_id, []).append(folder)

    def folder_out(folder: Folder) -> dict:
        node: dict[str, Any] = {"name": folder.name}
        subfolders = sorted(children.get(folder.id, []), key=lambda f: f.name.lower())
        if subfolders:
            node["folders"] = [folder_out(f) for f in subfolders]
        own = sorted(by_folder.get(folder.id, []), key=lambda s: s.name.lower())
        if own:
            node["sources"] = [_source_out(s) for s in own]
        return node

    customers_out = []
    for customer in sorted(session.exec(select(Customer)).all(), key=lambda c: c.name.lower()):
        node: dict[str, Any] = {"name": customer.name}
        top = sorted(
            (f for f in children.get(None, []) if f.customer_id == customer.id),
            key=lambda f: f.name.lower(),
        )
        if top:
            node["folders"] = [folder_out(f) for f in top]
        own = sorted(
            (s for s in by_folder.get(None, []) if s.customer_id == customer.id),
            key=lambda s: s.name.lower(),
        )
        if own:
            node["sources"] = [_source_out(s) for s in own]
        customers_out.append(node)

    doc: dict[str, Any] = {"version": SCHEMA_VERSION, "customers": customers_out}
    loose = sorted((s for s in sources if s.customer_id is None), key=lambda s: s.name.lower())
    if loose:
        doc["sources"] = [_source_out(s) for s in loose]
    return _EXPORT_HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
