import base64
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import winrm

from app.collectors import base as collector_base
from app.collectors.base import DirEntry
from app.crypto import decrypt_credential
from app.models import Rule, Source
from app.rules import is_entry_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy"]


# PowerShell accepts any of these as a single-quote string delimiter, not just
# the ASCII apostrophe -- escaping only "'" would leave the others free to end
# the quoted string early.
_PS_SINGLE_QUOTES = ("'", "\u2018", "\u2019", "\u201a", "\u201b")


def _ps_quote(value: str) -> str:
    """Escapes a value for embedding in a PowerShell single-quoted string by
    doubling every single-quote character it contains."""
    for quote in _PS_SINGLE_QUOTES:
        value = value.replace(quote, quote * 2)
    return "'" + value + "'"


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
    """Live directory listing through Get-ChildItem, filtered through the
    rule engine like the other connectors (files by the rule chain, folders
    by rules.is_dir_visible).

    The JSON comes back base64-encoded from UTF-8 bytes: PowerShell runs in
    a cmd shell whose stdout uses the console's OEM code page, so a single
    accented file name (`ação.log`) used to make the whole listing fail to
    decode (#168). Base64 sidesteps the code page entirely, the same way
    fetch_file already returns content."""
    path = _remote_path(source, relative_path)
    script = (
        f"$j = Get-ChildItem -LiteralPath {_ps_quote(path)} | "
        "Select-Object Name, PSIsContainer, Length | ConvertTo-Json -Compress; "
        "if ($j) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)) }"
    )
    output = _run_ps(source, script).strip()
    if not output:
        return []

    raw = json.loads(base64.b64decode(output).decode("utf-8"))
    if isinstance(raw, dict):
        raw = [raw]

    entries = []
    for item in raw:
        name = item["Name"]
        is_dir = bool(item["PSIsContainer"])
        child_path = f"{relative_path}/{name}" if relative_path else name
        if not is_entry_visible(child_path, is_dir, rules, case_insensitive=True):
            continue
        size = 0 if is_dir else int(item.get("Length") or 0)
        entries.append(DirEntry(name=name, path=child_path, is_dir=is_dir, size=size))
    return entries


# Each WinRM round trip returns one base64-encoded block, so a large file is
# read in ranges instead of loaded whole into memory on either end.
_CHUNK_BYTES = 4 * 1024 * 1024


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """WinRM has no native bulk file-transfer primitive, so content is read
    through PowerShell in byte ranges, base64-encoded, and reassembled here.
    The file is opened with FileShare.ReadWrite so a log another process is
    still writing can be read. Refuses anything over MAX_FETCH_MB before
    transferring a single block."""
    path = _ps_quote(_remote_path(source, relative_path))
    limit = collector_base.max_fetch_bytes()
    size = int(_run_ps(source, f"(Get-Item -LiteralPath {path}).Length").strip() or 0)
    if size > limit:
        raise collector_base.too_large(size, limit)

    written = 0
    try:
        with open(destination, "wb") as dst:
            while written < size:
                script = (
                    f"$f = [IO.File]::Open({path}, 'Open', 'Read', 'ReadWrite'); "
                    f"try {{ $f.Seek({written}, 'Begin') | Out-Null; "
                    f"$b = New-Object byte[] {_CHUNK_BYTES}; "
                    "$n = $f.Read($b, 0, $b.Length); "
                    "[Convert]::ToBase64String($b, 0, $n) } finally { $f.Close() }"
                )
                block = base64.b64decode(_run_ps(source, script).strip())
                if not block:
                    break  # the file shrank (rotated) while being read
                dst.write(block)
                written += len(block)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


@contextmanager
def local_copy(source: Source, relative_path: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path
