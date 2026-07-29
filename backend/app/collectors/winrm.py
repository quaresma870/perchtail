import base64
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import winrm

from app.collectors.base import DirEntry
from app.crypto import decrypt_credential
from app.models import Rule, Source
from app.rules import is_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy"]


def _ps_quote(value: str) -> str:
    """Escapes a value for embedding in a PowerShell single-quoted string
    (double the single quotes)."""
    return "'" + value.replace("'", "''") + "'"


def _session(source: Source) -> winrm.Session:
    """A fresh session per call — nothing kept open across requests, same
    fetch-on-open model as the other connectors."""
    creds = decrypt_credential(source.credential_ref)
    port = source.port or 5986
    scheme = "https" if port == 5986 else "http"
    endpoint = f"{scheme}://{source.host}:{port}/wsman"
    return winrm.Session(endpoint, auth=(creds["username"], creds["password"]), transport="ntlm")


def _run_ps(source: Source, script: str) -> str:
    result = _session(source).run_ps(script)
    if result.status_code != 0:
        raise RuntimeError(result.std_err.decode("utf-8", errors="replace"))
    return result.std_out.decode("utf-8")


def _remote_path(source: Source, relative_path: str = "") -> str:
    base = source.base_path.rstrip("\\")
    if not relative_path:
        return base
    return f"{base}\\{relative_path.replace('/', chr(92))}"


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Live directory listing filtered through the rule engine, via a
    JEA-constrained Get-ChildItem call (see docs/source-setup.md's WinRM
    section) — directories are always listed (never filtered), only files
    go through is_visible(), same rationale as the other connectors."""
    path = _remote_path(source, relative_path)
    script = (
        f"Get-ChildItem -LiteralPath {_ps_quote(path)} | "
        "Select-Object Name, PSIsContainer, Length | ConvertTo-Json -Compress"
    )
    output = _run_ps(source, script).strip()
    if not output:
        return []

    raw = json.loads(output)
    if isinstance(raw, dict):
        raw = [raw]

    entries = []
    for item in raw:
        name = item["Name"]
        is_dir = bool(item["PSIsContainer"])
        child_path = f"{relative_path}/{name}" if relative_path else name
        if not is_dir and not is_visible(child_path, rules):
            continue
        size = 0 if is_dir else int(item.get("Length") or 0)
        entries.append(DirEntry(name=name, path=child_path, is_dir=is_dir, size=size))
    return entries


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """WinRM has no native bulk file-transfer primitive, so content is
    base64-encoded through PowerShell (Get-Content) and decoded on our side
    — fine for log files, not efficient for very large ones. CLAUDE.md
    documents WinRM as the fallback for when SMB isn't open, not the
    primary path, so this tradeoff is acceptable here."""
    path = _remote_path(source, relative_path)
    script = f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({_ps_quote(path)}))"
    encoded = _run_ps(source, script).strip()
    destination.write_bytes(base64.b64decode(encoded))


@contextmanager
def local_copy(source: Source, relative_path: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path
