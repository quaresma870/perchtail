"""Names for audit events (#239). An audit entry that only says `source` and
an id is useless once that source is deleted, and misleading once it's
renamed, so app.audit.record_audit_event stores, when it writes the entry:

- `target_name`: the name of the object the event is about (a source's or
  customer's name, a username, a rule's pattern, ...);
- `target_context`: where it sits, when that helps tell it apart (a
  source's "Customer / folder / sub-folder", a rule's source, a grant's
  role);
- the name next to each id already in the metadata: `source_name` for
  `source_id`, `role_name` for `role_id`, `customer_name` for
  `customer_id`, `scope_name` for `scope_type` + `scope_id`.

Explicit metadata always wins over these. They're written into the entry
before it's chained, so they're covered by the hash chain like the rest.

The same lookups give the audit page a current name for entries written
before 0.5.3, which can't be backfilled (the hash chain covers them);
the page marks those as current names, not recorded ones."""

from sqlalchemy import inspect
from sqlmodel import Session, SQLModel, select

from app.auth.models import Role, RoleGrant, ScopeType, SSOGroupRoleMapping, SSOProviderConfig, User
from app.json_values import as_int
from app.logging_config import get_logger
from app.models import Alert, Customer, Folder, Rule, SeverityPattern, Source

logger = get_logger(__name__)

# Target types whose events never carry an id: there's nothing to name.
UNNAMED_TARGET_TYPES = frozenset({"system_settings", "monitoring_token", "audit_log", "backup"})

_MAX_FOLDER_DEPTH = 64


def _get(session: Session, model: type[SQLModel], row_id) -> SQLModel | None:
    """The row, also when it was deleted earlier in this same transaction:
    delete endpoints record their event right after session.delete(), and
    the name must still be found then."""
    if as_int(row_id) is None:
        return None
    for pending in session.deleted:
        identity = inspect(pending).identity  # no attribute load
        if isinstance(pending, model) and identity is not None and identity[0] == row_id:
            return pending
    return session.get(model, row_id)


def _folder_path(session: Session, folder_id: int | None) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    while folder_id is not None and folder_id not in seen and len(names) < _MAX_FOLDER_DEPTH:
        seen.add(folder_id)
        folder = _get(session, Folder, folder_id)
        if folder is None:
            break
        names.append(folder.name)
        folder_id = folder.parent_folder_id
    return list(reversed(names))


def _source_context(session: Session, source: Source) -> str | None:
    """ "Customer / folder / sub-folder" for a source, "system" for the
    built-in log source."""
    if source.is_system:
        return "system"
    customer = _get(session, Customer, source.customer_id)
    parts = ([customer.name] if customer else []) + _folder_path(session, source.folder_id)
    return " / ".join(parts) or None


def _source_name(session: Session, source_id) -> str | None:
    source = _get(session, Source, source_id)
    return source.name if source else None


def _role_name(session: Session, role_id) -> str | None:
    role = _get(session, Role, role_id)
    return role.name if role else None


def _scope_name(session: Session, scope_type, scope_id) -> str | None:
    model = {
        ScopeType.customer: Customer,
        ScopeType.folder: Folder,
        ScopeType.source: Source,
    }.get(scope_type)
    if model is None:
        return None
    row = _get(session, model, scope_id)
    return row.name if row else None


def _describe(session: Session, target_type: str, target_id) -> tuple[str | None, str | None]:
    """(name, context) of one target; (None, None) when it can't be found."""
    if target_type == "source":
        source = _get(session, Source, target_id)
        return (source.name, _source_context(session, source)) if source else (None, None)
    if target_type == "customer":
        customer = _get(session, Customer, target_id)
        return (customer.name, None) if customer else (None, None)
    if target_type == "folder":
        folder = _get(session, Folder, target_id)
        if folder is None:
            return None, None
        customer = _get(session, Customer, folder.customer_id)
        parts = ([customer.name] if customer else []) + _folder_path(
            session, folder.parent_folder_id
        )
        return folder.name, " / ".join(parts) or None
    if target_type == "user":
        user = _get(session, User, target_id)
        return (user.username, None) if user else (None, None)
    if target_type == "role":
        return _role_name(session, target_id), None
    if target_type == "role_grant":
        grant = _get(session, RoleGrant, target_id)
        if grant is None:
            return None, None
        scope = _scope_name(session, grant.scope_type, grant.scope_id) or f"#{grant.scope_id}"
        return f"{grant.scope_type} {scope}", _role_name(session, grant.role_id)
    if target_type == "rule":
        rule = _get(session, Rule, target_id)
        return (rule.pattern, _source_name(session, rule.source_id)) if rule else (None, None)
    if target_type == "severity_pattern":
        pattern = _get(session, SeverityPattern, target_id)
        if pattern is None:
            return None, None
        context = _source_name(session, pattern.source_id) if pattern.source_id else "global"
        return pattern.pattern, context
    if target_type == "alert":
        alert = _get(session, Alert, target_id)
        if alert is None:
            return None, None
        return alert.name, _source_name(session, alert.source_id) if alert.source_id else None
    if target_type == "sso_provider":
        provider = _get(session, SSOProviderConfig, target_id)
        return (provider.name, None) if provider else (None, None)
    if target_type == "sso_group_mapping":
        mapping = _get(session, SSOGroupRoleMapping, target_id)
        if mapping is None:
            return None, None
        return mapping.group_name, _role_name(session, mapping.role_id)
    return None, None


NAMED_TARGET_TYPES = frozenset(
    {
        "source",
        "customer",
        "folder",
        "user",
        "role",
        "role_grant",
        "rule",
        "severity_pattern",
        "alert",
        "sso_provider",
        "sso_group_mapping",
    }
)


_TARGET_MODELS: dict[str, type[SQLModel]] = {
    "source": Source,
    "customer": Customer,
    "folder": Folder,
    "user": User,
    "role": Role,
    "role_grant": RoleGrant,
    "rule": Rule,
    "severity_pattern": SeverityPattern,
    "alert": Alert,
    "sso_provider": SSOProviderConfig,
    "sso_group_mapping": SSOGroupRoleMapping,
}


def _load(session: Session, model: type[SQLModel], ids) -> list:
    ids = {row_id for row_id in ids if as_int(row_id) is not None}
    if not ids:
        return []
    return list(session.exec(select(model).where(model.id.in_(ids))).all())


def prefetch_targets(session: Session, targets) -> list:
    """Loads, in a handful of IN queries, every row describe_target needs
    for these (target_type, target_id) pairs: the targets, then what they
    point at (sources, roles, scopes, customers, and all the folders of
    those customers, for folder paths). describe_target's lookups then come
    from the session's identity map instead of several queries per target.
    The identity map holds rows weakly, so the caller keeps the returned
    list alive while it describes."""
    by_type: dict[str, set] = {}
    for target_type, target_id in targets:
        if target_type in _TARGET_MODELS and target_id is not None:
            by_type.setdefault(target_type, set()).add(target_id)
    loaded = {kind: _load(session, _TARGET_MODELS[kind], ids) for kind, ids in by_type.items()}
    keep = [row for rows in loaded.values() for row in rows]

    grants = loaded.get("role_grant", [])
    scope_ids = {kind: set() for kind in (ScopeType.customer, ScopeType.folder, ScopeType.source)}
    for grant in grants:
        if grant.scope_type in scope_ids:
            scope_ids[grant.scope_type].add(grant.scope_id)
    source_ids = scope_ids[ScopeType.source] | {
        row.source_id
        for kind in ("rule", "severity_pattern", "alert")
        for row in loaded.get(kind, [])
        if row.source_id is not None
    }
    sources = loaded.get("source", []) + _load(session, Source, source_ids)
    keep += sources
    keep += _load(
        session,
        Role,
        {
            row.role_id
            for kind in ("role_grant", "sso_group_mapping")
            for row in loaded.get(kind, [])
        },
    )
    folders = loaded.get("folder", []) + _load(session, Folder, scope_ids[ScopeType.folder])
    customer_ids = (
        {s.customer_id for s in sources if s.customer_id is not None}
        | {f.customer_id for f in folders}
        | scope_ids[ScopeType.customer]
    )
    keep += _load(session, Customer, customer_ids)
    if customer_ids:
        keep += session.exec(select(Folder).where(Folder.customer_id.in_(customer_ids))).all()
    return keep


def describe_target(session: Session, target_type: str | None, target_id) -> dict:
    """{"target_name", "target_context"} for one target, leaving out what
    can't be found. Never raises: naming must not stop an audit write."""
    if not target_type or target_type not in NAMED_TARGET_TYPES:
        return {}
    try:
        name, context = _describe(session, target_type, target_id)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("audit_labels.describe_failed", target_type=target_type, target_id=target_id)
        return {}
    labels = {}
    if name is not None:
        labels["target_name"] = name
    if context is not None:
        labels["target_context"] = context
    return labels


def audit_labels(
    session: Session, target_type: str | None, target_id, metadata: dict | None
) -> dict:
    """Everything record_audit_event adds to an entry's metadata: the
    target's name and context, and the names next to related ids.

    Runs without autoflush: a pending delete must not be flushed before the
    deleted row's name is read (an expired object reloads it from the
    database, where it's still there until the flush)."""
    with session.no_autoflush:
        return _audit_labels(session, target_type, target_id, metadata)


def _audit_labels(
    session: Session, target_type: str | None, target_id, metadata: dict | None
) -> dict:
    labels = describe_target(session, target_type, target_id)
    if not metadata:
        return labels
    try:
        if "source_id" in metadata and "source_name" not in metadata:
            name = _source_name(session, metadata["source_id"])
            if name is not None:
                labels["source_name"] = name
        if "role_id" in metadata and "role_name" not in metadata:
            name = _role_name(session, metadata["role_id"])
            if name is not None:
                labels["role_name"] = name
        if "customer_id" in metadata and "customer_name" not in metadata:
            customer = _get(session, Customer, metadata["customer_id"])
            if customer is not None:
                labels["customer_name"] = customer.name
        if "scope_type" in metadata and "scope_id" in metadata and "scope_name" not in metadata:
            name = _scope_name(session, metadata["scope_type"], metadata["scope_id"])
            if name is not None:
                labels["scope_name"] = name
    except Exception:  # noqa: BLE001 - naming must not stop an audit write
        logger.warning("audit_labels.related_failed", target_type=target_type)
    return labels
