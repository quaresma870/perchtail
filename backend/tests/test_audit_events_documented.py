"""Every audit event the code can record is documented in
docs/audit-events.md, and every event the page documents still exists.

The events are read from the code itself (AST, no import of the routers):
every `action` given to app.audit.record_audit_event (or to an AuditLog
built directly), followed through the
helpers that pass their own `action` argument on to it (as
app.auth_events._record does), through module constants (`PURGE_ACTION =
"audit.purge"`) and local variables (`action = "a" if x else "b"`), plus
the events a migration leaves in the audit outbox (app.audit_outbox.ACCEPTED).

A call whose action can't be followed that way fails the test until it's
listed in PASSTHROUGH_CALLS, with the reason, so a new event can't slip in
through an expression the extractor doesn't read.

The page documents an event by naming it, in backticks, in the first column
of a table under "## Events"."""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.audit_outbox import ACCEPTED

BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
AUDIT_EVENTS_DOC = Path(__file__).resolve().parents[2] / "docs" / "audit-events.md"

# What writes an audit entry: record_audit_event, and the AuditLog model
# itself (the key rotation builds its entry directly).
RECORD_FUNCTIONS = ("record_audit_event", "AuditLog")

# Calls to record_audit_event whose action isn't a literal, a constant or an
# argument of the enclosing function, as (path under backend/app, function).
PASSTHROUGH_CALLS = {
    # Records what a migration left in the outbox: only the actions in
    # ACCEPTED, which the test adds itself.
    ("audit_outbox.py", "drain_audit_outbox"),
}

ACTION_PATTERN = re.compile(r"^[a-z][a-z_]*(\.[a-z_]+)+$")


@dataclass
class _Extracted:
    actions: set[str] = field(default_factory=set)
    unresolved: set[tuple[str, str, int]] = field(default_factory=set)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _constants(node: ast.AST | None) -> list[str] | None:
    """The strings `node` can be, when it's a literal or a conditional of
    literals; None otherwise."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body, orelse = _constants(node.body), _constants(node.orelse)
        if body is not None and orelse is not None:
            return body + orelse
    return None


def _module_constants(tree: ast.Module) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            values = _constants(node.value)
            if isinstance(target, ast.Name) and values is not None:
                found[target.id] = values
    return found


def _local_assignments(function: ast.AST, name: str) -> list[ast.AST]:
    return [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = function.args
    return [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]


def _argument(call: ast.Call, name: str, position: int | None) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    if position is not None and position < len(call.args):
        return call.args[position]
    return None


def extract_actions(modules: dict[str, str]) -> _Extracted:
    """The audit actions recorded by `modules` ({path: source}). A call
    to record_audit_event, or to a helper that forwards one of its
    parameters as the action, gives its action when it's a literal, a
    conditional of literals, a module constant or a local variable
    assigned those. A forwarded parameter makes its function a helper, and
    every call to that helper is read the same way."""
    trees = {path: ast.parse(source) for path, source in modules.items()}
    constants = {path: _module_constants(tree) for path, tree in trees.items()}
    functions = {
        path: [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for path, tree in trees.items()
    }
    # helper name -> (parameter name, its position, module that defines it),
    # RECORD_FUNCTIONS first. A private helper (leading underscore) is only
    # matched by calls in its own module, so two modules' `_audit` helpers
    # are never mixed up.
    helpers: dict[str, set[tuple[str, int | None, str | None]]] = {
        name: {("action", None, None)} for name in RECORD_FUNCTIONS
    }
    result = _Extracted()

    def applies(name: str, defined_in: str | None, path: str) -> bool:
        return defined_in is None or not name.startswith("_") or defined_in == path

    def resolve(path: str, function, value: ast.AST | None) -> list[str] | str | None:
        """The strings, or the name of the enclosing function's parameter
        it forwards, or None when it can't be followed."""
        values = _constants(value)
        if values is not None:
            return values
        if not isinstance(value, ast.Name):
            return None
        if function is not None and value.id in _parameters(function):
            return value.id
        if function is not None:
            assigned = _local_assignments(function, value.id)
            if assigned:
                found = [_constants(v) for v in assigned]
                if all(f is not None for f in found):
                    return [s for f in found for s in f]
                return None
        return constants[path].get(value.id)

    changed = True
    while changed:
        changed = False
        for path, defs in functions.items():
            for function in defs:
                for call in ast.walk(function):
                    if not isinstance(call, ast.Call) or _call_name(call) not in helpers:
                        continue
                    name = _call_name(call)
                    for parameter, position, defined_in in list(helpers[name]):
                        if not applies(name, defined_in, path):
                            continue
                        value = _argument(call, parameter, position)
                        if value is None:
                            continue
                        resolved = resolve(path, function, value)
                        if isinstance(resolved, list):
                            result.actions.update(resolved)
                        elif isinstance(resolved, str):
                            names = _parameters(function)
                            if names and names[0] in ("self", "cls"):
                                names = names[1:]
                            entry = (resolved, names.index(resolved), path)
                            if entry not in helpers.setdefault(function.name, set()):
                                helpers[function.name].add(entry)
                                changed = True
                        elif (path, function.name) not in PASSTHROUGH_CALLS:
                            result.unresolved.add((path, function.name, call.lineno))
    return result


def malformed_actions(actions: set[str]) -> set[str]:
    """The actions that don't follow `area.verb`."""
    return {a for a in actions if not ACTION_PATTERN.match(a)}


def _app_modules() -> dict[str, str]:
    return {
        path.relative_to(BACKEND_APP).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(BACKEND_APP.rglob("*.py"))
    }


def documented_actions(markdown: str) -> set[str]:
    """The backticked names in the first column of the tables under
    "## Events", up to the next second-level heading."""
    section = markdown.split("\n## Events\n", 1)[1].split("\n## ", 1)[0]
    found: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        first_cell = line.split("|")[1]
        found.update(re.findall(r"`([^`]+)`", first_cell))
    return found


def _code_actions() -> set[str]:
    extracted = extract_actions(_app_modules())
    assert not extracted.unresolved, (
        "record_audit_event calls whose action the test can't read; pass a literal "
        f"or a constant, or list the function in PASSTHROUGH_CALLS: {sorted(extracted.unresolved)}"
    )
    return extracted.actions | set(ACCEPTED)


def test_every_recorded_event_is_documented():
    documented = documented_actions(AUDIT_EVENTS_DOC.read_text(encoding="utf-8"))
    missing = _code_actions() - documented
    assert not missing, f"audit events missing from docs/audit-events.md: {sorted(missing)}"


def test_every_recorded_event_follows_area_verb():
    malformed = malformed_actions(_code_actions())
    assert not malformed, f"audit action doesn't follow `area.verb`: {sorted(malformed)}"


def test_every_documented_event_is_recorded():
    documented = documented_actions(AUDIT_EVENTS_DOC.read_text(encoding="utf-8"))
    stale = documented - _code_actions()
    assert not stale, f"docs/audit-events.md documents events nothing records: {sorted(stale)}"


def test_extractor_follows_literals_helpers_constants_and_variables():
    source = """
DONE_ACTION = "thing.done"

def _record(session, action, *, reason):
    record_audit_event(session, user_id=None, action=action)

def _wrap(session, what_action):
    _record(session, what_action, reason="x")

def direct(session):
    record_audit_event(session, user_id=None, action="thing.create")

def through_helper(session):
    _record(session, "thing.failed", reason="r")
    _wrap(session, what_action="thing.wrapped")

def constant(session):
    record_audit_event(session, user_id=None, action=DONE_ACTION)

def variable(session, created):
    action = "thing.add" if created else "thing.change"
    record_audit_event(session, user_id=None, action=action)

def not_an_event(parser):
    parser.add_argument("--x", action="store_true")
"""
    extracted = extract_actions({"example.py": source})
    assert extracted.actions == {
        "thing.create",
        "thing.failed",
        "thing.wrapped",
        "thing.done",
        "thing.add",
        "thing.change",
    }
    assert not extracted.unresolved


def test_extractor_keeps_an_action_outside_area_verb():
    """An action that doesn't follow the pattern is still read, so it's
    reported as undocumented and as malformed instead of dropped."""
    source = """
def upgrade(session):
    record_audit_event(session, user_id=None, action="agent.upgrade_v2")
"""
    extracted = extract_actions({"example.py": source})
    assert extracted.actions == {"agent.upgrade_v2"}
    assert malformed_actions(extracted.actions) == {"agent.upgrade_v2"}
    documented = documented_actions(AUDIT_EVENTS_DOC.read_text(encoding="utf-8"))
    assert extracted.actions - documented == {"agent.upgrade_v2"}


def test_extractor_reports_an_action_it_cant_follow():
    source = """
def from_a_row(session, row):
    record_audit_event(session, user_id=None, action=row.action)
"""
    extracted = extract_actions({"example.py": source})
    assert extracted.unresolved == {("example.py", "from_a_row", 3)}


def test_an_undocumented_event_is_reported():
    """What test_every_recorded_event_is_documented checks, on an event
    the page doesn't have."""
    source = """
def new_feature(session):
    record_audit_event(session, user_id=None, action="widget.explode")
"""
    documented = documented_actions(AUDIT_EVENTS_DOC.read_text(encoding="utf-8"))
    assert extract_actions({"example.py": source}).actions - documented == {"widget.explode"}
