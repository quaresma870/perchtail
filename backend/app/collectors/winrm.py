import base64
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import winrm

from app.collectors import base as collector_base
from app.collectors import winrm_jea
from app.collectors.base import DirEntry, PathOutsideBaseError
from app.config import get_settings
from app.crypto import decrypt_credential
from app.logging_config import get_logger
from app.models import Rule, Source
from app.rules import is_entry_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "list_drives", "local_copy"]

logger = get_logger(__name__)


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


_DEFAULT_PORTS = {"https": 5986, "http": 5985}


def endpoint(source: Source) -> tuple[str, int]:
    """(scheme, port) for a WinRM source. An explicit scheme (#169) picks
    its own default port; with none set, the pre-0.5.0 rule applies: HTTPS
    on 5986 or with no port, plain HTTP on any other port."""
    if source.winrm_scheme in _DEFAULT_PORTS:
        return source.winrm_scheme, source.port or _DEFAULT_PORTS[source.winrm_scheme]
    port = source.port or 5986
    return ("https" if port == 5986 else "http"), port


def _ca_bundle_path(pem: str) -> str:
    """The source's CA certificate as a file for requests to verify against,
    written once per distinct certificate under DATA_DIR."""
    directory = Path(get_settings().data_dir) / "winrm_ca"
    path = directory / f"{hashlib.sha256(pem.encode()).hexdigest()}.pem"
    if not path.exists():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(pem)
        os.replace(tmp, path)
    return str(path)


def session_target(source: Source) -> tuple[str, dict]:
    """The WSMan URL and the TLS options for a source's session."""
    scheme, port = endpoint(source)
    kwargs: dict = {"transport": "ntlm", "server_cert_validation": "validate"}
    if scheme == "https" and source.tls_ca_pem:
        kwargs["ca_trust_path"] = _ca_bundle_path(source.tls_ca_pem)
    return f"{scheme}://{source.host}:{port}/wsman", kwargs


def _session(source: Source) -> winrm.Session:
    """A fresh session per call — nothing kept open across requests, same
    fetch-on-open model as the other connectors."""
    collector_base.require_credential(source)
    creds = decrypt_credential(source.credential_ref)
    target, kwargs = session_target(source)
    return winrm.Session(target, auth=(creds["username"], creds["password"]), **kwargs)


_OUTSIDE_BASE = "PERCHTAIL_OUTSIDE_BASE"
_NOT_A_DIRECTORY = "PERCHTAIL_NOT_A_DIRECTORY"


def _run_ps(source: Source, script: str) -> str:
    result = _session(source).run_ps(script)
    out = result.std_out.decode("utf-8", errors="replace")
    if result.status_code != 0:
        if _OUTSIDE_BASE in out:
            raise PathOutsideBaseError("path outside the base path")
        if _NOT_A_DIRECTORY in out:
            raise NotADirectoryError("not a directory")
        raise RuntimeError(result.std_err.decode("utf-8", errors="replace"))
    return out


def _containment_guard(source: Source, relative_path: str) -> str:
    """PowerShell that stops the script when any component below the base
    path is a junction or symlink. [IO.File]::GetAttributes reads the
    attributes of the link itself rather than its target, so nothing is
    followed. Get-ChildItem, Get-Item and [IO.File]::Open all follow them,
    which let a junction in the log folder expose anything the account can
    read. The base path itself is the operator's choice and not checked."""
    parts = [p for p in relative_path.split("/") if p]
    if not parts:
        return ""
    prefixes = [_remote_path(source, "/".join(parts[:i])) for i in range(1, len(parts) + 1)]
    quoted = ", ".join(_ps_quote(p) for p in prefixes)
    return (
        f"foreach ($__c in @({quoted})) {{ "
        "if ([IO.File]::GetAttributes($__c) -band [IO.FileAttributes]::ReparsePoint) "
        f"{{ Write-Output '{_OUTSIDE_BASE}'; exit 3 }} }}; "
    )


def _remote_path(source: Source, relative_path: str = "") -> str:
    base = source.base_path.rstrip("\\")
    if not relative_path:
        # A bare "D:" is the current directory on D: in PowerShell, not the
        # drive's root.
        return base + "\\" if len(base) == 2 and base[1] == ":" else base
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
    if source.winrm_configuration:
        return winrm_jea.list_directory(source, rules, relative_path)
    path = _remote_path(source, relative_path)
    # Junctions and symlinks are left out of the listing (never followed),
    # and listing a file is "not found" rather than the file itself.
    script = (
        _containment_guard(source, relative_path)
        + f"if (-not [IO.Directory]::Exists({_ps_quote(path)})) "
        f"{{ Write-Output '{_NOT_A_DIRECTORY}'; exit 4 }}; "
        f"$j = Get-ChildItem -LiteralPath {_ps_quote(path)} | "
        "Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } | "
        # MTime (#273): last write, in Unix milliseconds (UTC).
        "Select-Object Name, PSIsContainer, Length, "
        "@{n='MTime';e={[DateTimeOffset]::new($_.LastWriteTimeUtc).ToUnixTimeMilliseconds()}} "
        "| ConvertTo-Json -Compress; "
        "if ($j) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)) }"
    )
    try:
        output = _run_ps(source, script).strip()
    except PathOutsideBaseError:
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    return parse_listing(output, relative_path, rules)


def parse_listing(output: str, relative_path: str, rules: list[Rule]) -> list[DirEntry]:
    """A listing's base64 UTF-8 JSON (from the script above, or from the JEA
    role's Get-PerchTailListing) as rule-filtered entries."""
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
        mtime_ms = item.get("MTime")
        mtime = mtime_ms / 1000 if isinstance(mtime_ms, int | float) else None
        entries.append(DirEntry(name=name, path=child_path, is_dir=is_dir, size=size, mtime=mtime))
    return entries


def list_drives(source: Source) -> list[str]:
    """The host's file-system drive roots (`C:\\`, `D:\\`), for the Base path
    picker (#159). Behind a JEA endpoint (#169): the folders its role allows."""
    if source.winrm_configuration:
        return winrm_jea.list_drives(source)
    script = (
        "$j = Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Root | "
        "ConvertTo-Json -Compress; "
        "if ($j) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)) }"
    )
    output = _run_ps(source, script).strip()
    if not output:
        return []
    raw = json.loads(base64.b64decode(output).decode("utf-8"))
    return sorted([raw] if isinstance(raw, str) else raw)


# Each WinRM round trip returns one base64-encoded block, so a large file is
# read in ranges instead of loaded whole into memory on either end.
_CHUNK_BYTES = 4 * 1024 * 1024


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """WinRM has no native bulk file-transfer primitive, so content is read
    through PowerShell in byte ranges, base64-encoded, and reassembled here.
    The file is opened with FileShare.ReadWrite so a log another process is
    still writing can be read. Refuses anything over MAX_FETCH_MB before
    transferring a single block."""
    if source.winrm_configuration:
        return winrm_jea.fetch_file(source, relative_path, destination)
    path = _ps_quote(_remote_path(source, relative_path))
    guard = _containment_guard(source, relative_path)
    limit = collector_base.max_fetch_bytes()
    try:
        size = int(_run_ps(source, guard + f"(Get-Item -LiteralPath {path}).Length").strip() or 0)
    except PathOutsideBaseError:
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    if size > limit:
        raise collector_base.too_large(size, limit)

    written = 0
    try:
        with open(destination, "wb") as dst:
            while written < size:
                # The guard runs again with every block, so a link swapped in
                # after the first check is refused too.
                script = guard + (
                    f"$f = [IO.File]::Open({path}, 'Open', 'Read', 'ReadWrite'); "
                    f"try {{ $f.Seek({written}, 'Begin') | Out-Null; "
                    f"$b = New-Object byte[] {_CHUNK_BYTES}; "
                    "$n = $f.Read($b, 0, $b.Length); "
                    "[Convert]::ToBase64String($b, 0, $n) } finally { $f.Close() }"
                )
                try:
                    block = base64.b64decode(_run_ps(source, script).strip())
                except PathOutsideBaseError:
                    # A link swapped in mid-read: logged like the first check (#206).
                    logger.warning(
                        "winrm.path_outside_base",
                        source_id=source.id,
                        path=relative_path,
                        offset=written,
                    )
                    raise
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


def read_range(
    source: Source, relative_path: str, offset: int, length: int, head: int = 0
) -> collector_base.RangeRead:
    """Incremental follow (#254): the file's size, its first `head` bytes and
    up to `length` bytes from `offset`, in one PowerShell round trip that
    seeks before reading, so nothing else is transferred."""
    if source.winrm_configuration:
        return winrm_jea.read_range(source, relative_path, offset, length, head)
    path = _ps_quote(_remote_path(source, relative_path))
    guard = _containment_guard(source, relative_path)
    offset = max(int(offset), 0)
    length = max(min(int(length), collector_base.RANGE_MAX_BYTES), 0)
    head = max(min(int(head), collector_base.RANGE_MAX_HEAD_BYTES), 0)
    script = guard + (
        f"$f = [IO.File]::Open({path}, 'Open', 'Read', 'ReadWrite'); "
        "try { $len = $f.Length; "
        f"$h = New-Object byte[] {head}; $hn = 0; "
        # Read() may return less than asked: loop until the head is full or
        # the file ends, or a short head would look like a rotated file.
        "while ($hn -lt $h.Length) { $r = $f.Read($h, $hn, $h.Length - $hn); "
        "if ($r -le 0) { break }; $hn += $r }; "
        f"$b = New-Object byte[] {length}; $n = 0; "
        f"if ({offset} -lt $len) {{ $f.Seek({offset}, 'Begin') | Out-Null; "
        "$n = $f.Read($b, 0, $b.Length) }; "
        '"$len|" + [Convert]::ToBase64String($h, 0, $hn) + "|" + '
        "[Convert]::ToBase64String($b, 0, $n) } finally { $f.Close() }"
    )
    try:
        output = _run_ps(source, script).strip()
    except PathOutsideBaseError:
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    return parse_range(output)


def parse_range(output: str) -> collector_base.RangeRead:
    """ "size|head|data" (base64 parts), from the script above or the JEA
    role's Read-PerchTailRange."""
    try:
        size_text, head_b64, data_b64 = output.splitlines()[-1].split("|", 2)
        return collector_base.RangeRead(
            int(size_text), base64.b64decode(head_b64), base64.b64decode(data_b64)
        )
    except (ValueError, IndexError) as exc:
        raise collector_base.SourceError(
            "unexpected answer from PowerShell",
            public_message="The source sent an unexpected answer",
        ) from exc
