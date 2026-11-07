"""Several base paths in one source (#161).

A source normally reads one folder, `Source.base_path`, and its tree is that
folder. With two or more roots configured (`Source.roots`, a list of
`{"label", "path"}`), the tree's top level is one folder per root, named by
its label, and everything below `label/` is read from that root's path.

Rules see the same virtual paths the viewer does (`iis/W3SVC1/u_ex.log`),
so `**/*.log` still covers every root and `iis/**` targets one. Collectors
are called per root with an allow-all rule chain and the real chain is
applied here on the virtual path -- one rule evaluation, not two that could
disagree.

`base_path` keeps the first root's path, so code that only needs "where
does this source point" (the overview, audit, push agents) still works. Push
agents serve a single folder and can't have more than one root."""

import ntpath
import posixpath
import re
from dataclasses import replace
from types import ModuleType

from app.collectors.base import DirEntry, SourceNotFoundError
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from app.rules import is_entry_visible, rules_case_insensitive

# The admin-configurable limit (Settings -> System, `source_max_roots`)
# can be set anywhere from 1 (several folders turned off) to this hard cap.
DEFAULT_MAX_ROOTS = 20
HARD_MAX_ROOTS = 100
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

ALLOW_ALL = [Rule(order=0, type=RuleType.include, pattern="**", pattern_kind=PatternKind.glob)]


def roots_of(source: Source) -> list[dict]:
    """The configured roots, or [] for an ordinary single-folder source."""
    roots = source.roots or []
    return roots if len(roots) >= 2 else []


def max_roots(session) -> int:
    """The configured per-source folder limit, clamped to 1..HARD_MAX_ROOTS."""
    from app import system_settings

    return max(1, min(system_settings.get_int(session, "source_max_roots"), HARD_MAX_ROOTS))


def roots_limit_message(limit: int) -> str:
    if limit <= 1:
        return "Several folders per source are turned off (Settings → System)"
    return f"A source can read at most {limit} folders (Settings → System)"


def is_multi_root(source: Source) -> bool:
    return bool(roots_of(source))


def valid_label(label: str) -> bool:
    return bool(_LABEL_RE.match(label)) and label not in (".", "..") and not label.endswith(".")


def with_base_path(source: Source, base_path: str) -> Source:
    """A detached copy of `source` pointing at another folder. Never added
    to a session, so nothing about it is ever written back."""
    return Source(
        id=source.id,
        name=source.name,
        customer_id=source.customer_id,
        folder_id=source.folder_id,
        protocol=source.protocol,
        host=source.host,
        port=source.port,
        credential_ref=source.credential_ref,
        base_path=base_path,
        enabled=source.enabled,
        is_system=source.is_system,
        agent_token_hash=source.agent_token_hash,
        winrm_scheme=source.winrm_scheme,
        winrm_configuration=source.winrm_configuration,
        tls_ca_pem=source.tls_ca_pem,
    )


def split(source: Source, path: str) -> tuple[Source, str]:
    """Maps a virtual path (`label/rest`) to the root's own source and the
    path inside it. Unknown labels are simply not found."""
    label, _, rest = path.partition("/")
    for root in roots_of(source):
        if root["label"] == label:
            return with_base_path(source, root["path"]), rest
    raise SourceNotFoundError(f"no root labelled {label!r}")


def list_directory(
    module: ModuleType, source: Source, rules: list[Rule], relative_path: str = ""
) -> list[DirEntry]:
    case_insensitive = rules_case_insensitive(source)
    if not relative_path:
        return [
            DirEntry(name=root["label"], path=root["label"], is_dir=True, size=0)
            for root in roots_of(source)
            if is_entry_visible(root["label"], True, rules, case_insensitive=case_insensitive)
        ]
    rooted, rest = split(source, relative_path)
    label = relative_path.partition("/")[0]
    entries = []
    for entry in module.list_directory(rooted, ALLOW_ALL, rest):
        full = f"{label}/{entry.path}"
        if is_entry_visible(full, entry.is_dir, rules, case_insensitive=case_insensitive):
            entries.append(replace(entry, path=full))
    return entries


def normalize_root_path(protocol: Protocol, path: str) -> str:
    path = path.strip()
    if protocol in (Protocol.smb, Protocol.winrm):
        return path.rstrip("\\/") if not re.fullmatch(r"[A-Za-z]:[\\/]?", path) else path[:2] + "\\"
    if protocol in (Protocol.ssh, Protocol.local):
        return posixpath.normpath(path) if path else path
    return path


def same_path(protocol: Protocol, a: str, b: str) -> bool:
    if protocol in (Protocol.smb, Protocol.winrm):
        return ntpath.normcase(a) == ntpath.normcase(b)
    return a == b
