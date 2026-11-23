import base64
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import winrm
from winrm import exceptions as winrm_exc
from winrm.protocol import Protocol as WinRMProtocol

from app.collectors import base as collector_base
from app.collectors import winrm_jea
from app.collectors.base import DirEntry, PathOutsideBaseError, SourceNotAFileError
from app.collectors.ranged import RemoteFileChangedError, check_length, check_size
from app.config import get_settings
from app.crypto import decrypt_credential
from app.logging_config import get_logger
from app.models import Rule, Source
from app.rules import is_entry_visible
from app.scratch import scratch_temp_dir

__all__ = ["DirEntry", "fetch_file", "list_directory", "list_drives", "local_copy", "open_ranged"]

# open_ranged reads a .zip in place (collectors/ranged.py), over WinRS and
# behind a JEA endpoint alike.
RANGED_READS = True

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


# The WS-Man envelope size every request asks for. pywinrm and pypsrp both
# default to 150 KiB, the limit of Windows Server 2008; WinRM 3.0 (Server 2012
# and later) accepts 500 KiB unless an administrator lowered MaxEnvelopeSizekb.
# A larger envelope carries more output per Receive, and the number of
# Receive round trips is what makes a large read slow (T-0074).
ENVELOPE_SIZE = 512000
FALLBACK_ENVELOPE_SIZE = 153600


class _Protocol(WinRMProtocol):
    """pywinrm's Protocol with the envelope size it asks for set per
    session: pywinrm 0.5.0 writes a fixed 153600 into every header and
    ignores its own max_env_sz."""

    def __init__(self, *args, envelope_size: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_env_sz = envelope_size

    def build_wsman_header(self, *args, **kwargs):
        header = super().build_wsman_header(*args, **kwargs)
        header["env:Header"]["w:MaxEnvelopeSize"]["#text"] = str(self.max_env_sz)
        return header


class _Session(winrm.Session):
    def __init__(self, target: str, auth: tuple[str, str], envelope_size: int, **kwargs):
        username, password = auth
        self.url = self._build_url(target, kwargs.get("transport", "plaintext"))
        self.protocol = _Protocol(
            self.url, username=username, password=password, envelope_size=envelope_size, **kwargs
        )


def _session(source: Source, envelope_size: int) -> winrm.Session:
    """A fresh session per call — nothing kept open across requests, same
    fetch-on-open model as the other connectors."""
    collector_base.require_credential(source)
    creds = decrypt_credential(source.credential_ref)
    target, kwargs = session_target(source)
    return _Session(
        target, auth=(creds["username"], creds["password"]), envelope_size=envelope_size, **kwargs
    )


def is_envelope_refusal(exc: BaseException) -> bool:
    """Whether a WS-Man fault (pywinrm's or pypsrp's) refuses the envelope
    size asked for: the wsman:EncodingLimit fault, or a fault whose text
    names the envelope size. Any other fault is not an envelope refusal."""
    from pypsrp import exceptions as psrp_exc

    if isinstance(exc, winrm_exc.WSManFaultError):
        parts = (exc.fault_subcode, exc.reason, exc.response)
    elif isinstance(exc, psrp_exc.WSManFaultError):
        parts = (exc.code, exc.reason, exc.provider_fault)
    else:
        return False
    text = " ".join(str(part) for part in parts if part).lower()
    return "encodinglimit" in text or "maxenvelopesize" in text or "envelope size" in text


def with_envelope_fallback(source: Source, attempt, envelope_size: int = ENVELOPE_SIZE):
    """Runs `attempt(envelope_size)`; when the server refuses that envelope
    size, runs it once more with FALLBACK_ENVELOPE_SIZE. Returns the result
    and the envelope size that worked, so later requests of the same
    operation can start from it."""
    if envelope_size != FALLBACK_ENVELOPE_SIZE:
        try:
            return attempt(envelope_size), envelope_size
        except Exception as exc:
            if not is_envelope_refusal(exc):
                raise
        logger.warning("winrm.envelope_fallback", source_id=source.id)
    return attempt(FALLBACK_ENVELOPE_SIZE), FALLBACK_ENVELOPE_SIZE


_OUTSIDE_BASE = "PERCHTAIL_OUTSIDE_BASE"
_NOT_A_DIRECTORY = "PERCHTAIL_NOT_A_DIRECTORY"
_NOT_FOUND = "PERCHTAIL_NOT_FOUND"
_IS_A_DIRECTORY = "PERCHTAIL_IS_A_DIRECTORY"
_CHANGED = "PERCHTAIL_CHANGED"
_END = "PERCHTAIL_END"


def _raise_for_marker(out: str) -> None:
    if _OUTSIDE_BASE in out:
        raise PathOutsideBaseError("path outside the base path")
    if _NOT_A_DIRECTORY in out:
        raise NotADirectoryError("not a directory")
    if _IS_A_DIRECTORY in out:
        # A folder to read as a file. WinRM refuses every link, so there is
        # no target to check the rules on.
        raise SourceNotAFileError("is a directory")
    if _NOT_FOUND in out:
        raise FileNotFoundError("not found")
    if _CHANGED in out:
        raise RemoteFileChangedError("the file's size changed while it was read")


def _run_ps_once(source: Source, script: str, envelope_size: int) -> str:
    result = _session(source, envelope_size).run_ps(script)
    out = result.std_out.decode("utf-8", errors="replace")
    if result.status_code != 0:
        _raise_for_marker(out)
        raise RuntimeError(result.std_err.decode("utf-8", errors="replace"))
    return out


def _run_ps_envelope(
    source: Source, script: str, envelope_size: int = ENVELOPE_SIZE
) -> tuple[str, int]:
    return with_envelope_fallback(
        source, lambda size: _run_ps_once(source, script, size), envelope_size
    )


def _run_ps(source: Source, script: str) -> str:
    return _run_ps_envelope(source, script)[0]


def _require_file(path: str) -> str:
    """PowerShell that stops the script when `path` (already quoted) isn't
    a file. Without it a missing file failed only with non-terminating
    errors, the script still exited 0 with no output, and the file read as
    empty instead of "not found" (#272, found in the 0.6.0-rc.1 lab). A
    folder is said apart (SourceNotAFileError), so its refusal is audited
    as one, though answered the same way."""
    return (
        f"if ([IO.Directory]::Exists({path})) {{ Write-Output '{_IS_A_DIRECTORY}'; exit 6 }}; "
        f"if (-not [IO.File]::Exists({path})) {{ Write-Output '{_NOT_FOUND}'; exit 5 }}; "
    )


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


# An absolute path on a drive (D:\Logs). Anything else -- a UNC path
# (\\server\share) or a device path (\\?\..., \\.\...) -- would have the WinRM
# host reach out to another machine with its own credentials.
DRIVE_PATH = re.compile(r"^[A-Za-z]:([\\/]|$)")


class BasePathNotOnDriveError(collector_base.SourceError):
    public_message = (
        "This source's Base path isn't a path on a drive (e.g. D:\\Logs): "
        "edit the source to correct it"
    )


def _remote_path(source: Source, relative_path: str = "") -> str:
    if not DRIVE_PATH.match(source.base_path or ""):
        # Refused when saved since 0.7.0; a source saved before may still
        # have one, and is never read until it is corrected.
        logger.warning("winrm.base_path_not_on_drive", source_id=source.id)
        raise BasePathNotOnDriveError("the Base path is not a path on a drive")
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


# The streaming read writes one base64 line per sub-block. 3 MiB is a
# multiple of 3, so no line but the last carries base64 padding, and a line
# (4 MiB of text) stays well below the shell's memory quota.
_SUB_BLOCK_BYTES = 3 * 1024 * 1024
# The longest line the read script can write, with room for the line end:
# anything longer is not its output.
_MAX_LINE_CHARS = (_SUB_BLOCK_BYTES + 2) // 3 * 4 + 2
# What stderr is kept of a failed read, for the error message.
_MAX_STDERR_BYTES = 64 * 1024


class _UnexpectedOutput(collector_base.SourceError):
    public_message = "The source sent an unexpected answer"


def _read_script(source: Source, relative_path: str, size: int) -> str:
    """PowerShell that checks the path, opens the file once and writes it
    as base64 lines of up to _SUB_BLOCK_BYTES bytes each, up to `size` bytes,
    then `PERCHTAIL_END <bytes read>`. The containment guard runs before the
    file is opened and the whole read goes through that one handle, so a
    link swapped in afterwards can't divert it. Read() may return less than
    asked, so each sub-block is read until it is full or the file ends; a
    file that shrank (rotated) ends the read early, and one that grew isn't
    read past `size`."""
    path = _ps_quote(_remote_path(source, relative_path))
    return (
        _containment_guard(source, relative_path)
        + _require_file(path)
        + (
            f"$f = [IO.File]::Open({path}, 'Open', 'Read', 'ReadWrite'); "
            f"try {{ $left = [long]{int(size)}; $t = [long]0; "
            f"$b = New-Object byte[] {_SUB_BLOCK_BYTES}; "
            "while ($left -gt 0) { $want = [int][Math]::Min($left, [long]$b.Length); $n = 0; "
            "while ($n -lt $want) { $r = $f.Read($b, $n, $want - $n); "
            "if ($r -le 0) { break }; $n += $r }; "
            "if ($n -le 0) { break }; "
            "[Convert]::ToBase64String($b, 0, $n); $t += $n; $left -= $n; "
            "if ($n -lt $want) { break } }; "
            f'"{_END} $t" }} finally {{ $f.Close() }}'
        )
    )


def _encoded_command(script: str) -> str:
    """The command line Session.run_ps sends for a script."""
    return "powershell -encodedcommand " + base64.b64encode(script.encode("utf_16_le")).decode(
        "ascii"
    )


class _ReadStream:
    """Turns the read script's stdout, as it arrives, into bytes written to
    `dst`: only whole lines are decoded, so what is held in memory is at most
    one unfinished line plus the response being handled."""

    def __init__(self, dst, size: int):
        self.dst = dst
        self.size = size
        self.pending = b""
        self.written = 0
        self.end: int | None = None

    def feed(self, data: bytes) -> None:
        self.pending += data
        *lines, self.pending = self.pending.split(b"\n")
        if len(self.pending) > _MAX_LINE_CHARS:
            raise _UnexpectedOutput("a line longer than a read block")
        for line in lines:
            self._line(line.strip())

    def finish(self) -> None:
        self._line(self.pending.strip())
        self.pending = b""

    def _line(self, line: bytes) -> None:
        if not line:
            return
        text = line.decode("ascii", errors="replace")
        # A marker written before any data stops the read like it stops _run_ps.
        _raise_for_marker(text)
        if self.end is not None:
            raise _UnexpectedOutput("output after the end marker")
        if text.startswith(_END + " "):
            try:
                self.end = int(text[len(_END) + 1 :])
            except ValueError as exc:
                raise _UnexpectedOutput("an unreadable end marker") from exc
            return
        try:
            block = base64.b64decode(line, validate=True)
        except ValueError as exc:
            raise _UnexpectedOutput("a line that isn't base64") from exc
        if self.written + len(block) > self.size:
            raise _UnexpectedOutput("more bytes than the file's size")
        self.dst.write(block)
        self.written += len(block)


def _close_quietly(call, *args, **kwargs) -> None:
    """Best-effort cleanup: a failure to end the command or close the shell
    must not hide the read's own result or error."""
    with suppress(Exception):
        call(*args, **kwargs)


def _command_output(session, shell_id: str, script: str, feed) -> tuple[int, int, bytes]:
    """Runs `script` in the open shell `shell_id`, handing its stdout to
    `feed` one Receive at a time (Protocol.get_command_output_raw). Returns
    how many Receives it took, the exit code and (the start of) stderr,
    for _raise_for_exit once the output's markers were looked at."""
    protocol = session.protocol
    responses = 0
    stderr = b""
    command_id = protocol.run_command(shell_id, _encoded_command(script))
    try:
        done, code = False, -1
        while not done:
            try:
                out, err, code, done = protocol.get_command_output_raw(shell_id, command_id)
            except winrm_exc.WinRMOperationTimeoutError:
                continue  # no output yet: ask again, as pywinrm does
            responses += 1
            if err and len(stderr) < _MAX_STDERR_BYTES:
                stderr = (stderr + err)[:_MAX_STDERR_BYTES]
            feed(out)
    finally:
        _close_quietly(protocol.cleanup_command, shell_id, command_id)
    return responses, code, stderr


def _raise_for_exit(session, code: int, stderr: bytes) -> None:
    if code != 0:
        raise RuntimeError(session._clean_error_msg(stderr).decode("utf-8", errors="replace"))


def _stream_command(session, shell_id: str, script: str, dst, size: int) -> tuple[int, int]:
    """A read script (_read_script, _ranged_read_script) run in the open
    shell, its base64 lines written to `dst` as they arrive (_ReadStream),
    at most `size` bytes. Returns (bytes written, Receives)."""
    stream = _ReadStream(dst, size)
    responses, code, stderr = _command_output(session, shell_id, script, stream.feed)
    stream.finish()
    _raise_for_exit(session, code, stderr)
    if stream.end is None:
        raise _UnexpectedOutput("the read ended without its end marker")
    if stream.end != stream.written:
        raise _UnexpectedOutput(
            f"the read reported {stream.end} bytes but {stream.written} arrived"
        )
    return stream.written, responses


def _stream_read(
    source: Source, relative_path: str, size: int, destination: Path, envelope_size: int
) -> None:
    """One shell and one PowerShell process for the whole content, read
    with one Receive at a time and written to `destination` as it
    arrives."""
    session = _session(source, envelope_size)
    protocol = session.protocol
    started = time.monotonic()
    try:
        shell_id = protocol.open_shell()
        try:
            with open(destination, "wb") as dst:
                written, responses = _stream_command(
                    session, shell_id, _read_script(source, relative_path, size), dst, size
                )
        finally:
            _close_quietly(protocol.close_shell, shell_id, close_session=False)
    finally:
        _close_quietly(protocol.transport.close_session)
    logger.debug(
        "winrm.fetch_done",
        source_id=source.id,
        bytes=written,
        responses=responses,
        seconds=round(time.monotonic() - started, 3),
        envelope_size=envelope_size,
    )


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """WinRM has no native bulk file-transfer primitive, so content is read
    through PowerShell as base64 and reassembled here: one request for the
    size, then one shell and one PowerShell process that streams the whole
    file (_stream_read). The file is opened with FileShare.ReadWrite so a log
    another process is still writing can be read. Refuses anything over
    MAX_FETCH_MB before transferring a single block."""
    if source.winrm_configuration:
        return winrm_jea.fetch_file(source, relative_path, destination)
    path = _ps_quote(_remote_path(source, relative_path))
    guard = _containment_guard(source, relative_path)
    limit = collector_base.max_fetch_bytes()
    try:
        size_text, envelope_size = _run_ps_envelope(
            source, guard + _require_file(path) + f"(Get-Item -LiteralPath {path}).Length"
        )
    except PathOutsideBaseError:
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    size = int(size_text.strip() or 0)
    if size > limit:
        raise collector_base.too_large(size, limit)

    try:
        with_envelope_fallback(
            source,
            lambda envelope: _stream_read(source, relative_path, size, destination, envelope),
            envelope_size,
        )
    except PathOutsideBaseError:
        destination.unlink(missing_ok=True)
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


@contextmanager
def local_copy(source: Source, relative_path: str):
    with scratch_temp_dir() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path


def _ranged_read_script(
    source: Source, relative_path: str, offset: int, length: int, size: int
) -> str:
    """_read_script for a ranged read: up to `length` bytes from `offset`,
    streamed as base64 lines, and refused with PERCHTAIL_CHANGED when the
    file's length isn't `size` when it is opened, before each sub-block or
    at the end."""
    path = _ps_quote(_remote_path(source, relative_path))
    changed = f"if ($f.Length -ne $size) {{ Write-Output '{_CHANGED}'; exit 7 }}; "
    return (
        _containment_guard(source, relative_path)
        + _require_file(path)
        + (
            f"$f = [IO.File]::Open({path}, 'Open', 'Read', 'ReadWrite'); "
            f"try {{ $size = [long]{int(size)}; "
            + changed
            + f"$f.Seek([long]{int(offset)}, 'Begin') | Out-Null; "
            f"$left = [long]{int(length)}; $t = [long]0; "
            f"$b = New-Object byte[] {_SUB_BLOCK_BYTES}; "
            "while ($left -gt 0) { "
            + changed
            + "$want = [int][Math]::Min($left, [long]$b.Length); $n = 0; "
            "while ($n -lt $want) { $r = $f.Read($b, $n, $want - $n); "
            "if ($r -le 0) { break }; $n += $r }; "
            "if ($n -le 0) { break }; "
            "[Convert]::ToBase64String($b, 0, $n); $t += $n; $left -= $n; "
            "if ($n -lt $want) { break } }; " + changed + f'"{_END} $t" }} finally {{ $f.Close() }}'
        )
    )


def _read_at_script(source: Source, relative_path: str, offset: int, length: int) -> str:
    """PowerShell that writes "size before|size after|data" (data as
    base64): up to `length` bytes from `offset`, read until full or the end
    of the file, like read_range's script."""
    path = _ps_quote(_remote_path(source, relative_path))
    return (
        _containment_guard(source, relative_path)
        + _require_file(path)
        + (
            f"$f = [IO.File]::Open({path}, 'Open', 'Read', 'ReadWrite'); "
            "try { $len = $f.Length; "
            f"$b = New-Object byte[] {int(length)}; $n = 0; "
            f"if ({int(offset)} -lt $len) {{ $f.Seek([long]{int(offset)}, 'Begin') | Out-Null; "
            "while ($n -lt $b.Length) { $r = $f.Read($b, $n, $b.Length - $n); "
            "if ($r -le 0) { break }; $n += $r } }; "
            '"$len|" + $f.Length + "|" + [Convert]::ToBase64String($b, 0, $n) '
            "} finally { $f.Close() }"
        )
    )


class _WinRSRemote:
    """A RangedRemote over one WinRM connection and one shell, for the
    length of an operation: each read is one PowerShell process in that
    shell that checks the path again (containment, a file), opens it and
    reads at the offset. A small read answers in one Receive
    (_read_at_script); a long one is streamed (_ranged_read_script). Both
    refuse a file whose size isn't the one it had when it was opened."""

    def __init__(self, source: Source, relative_path: str, size: int, session, shell_id: str):
        self._source = source
        self._relative_path = relative_path
        self._session = session
        self._shell_id = shell_id
        self.size = size

    def read_at(self, offset: int, length: int) -> bytes:
        parts: list[bytes] = []
        _, code, stderr = _command_output(
            self._session,
            self._shell_id,
            _read_at_script(self._source, self._relative_path, offset, length),
            parts.append,
        )
        output = b"".join(parts).decode("ascii", errors="replace").strip()
        _raise_for_marker(output)
        _raise_for_exit(self._session, code, stderr)
        try:
            before, after, data_b64 = output.splitlines()[-1].split("|", 2)
            sizes = (int(before), int(after))
            data = base64.b64decode(data_b64, validate=True)
        except (ValueError, IndexError) as exc:
            raise _UnexpectedOutput("an unreadable ranged read") from exc
        for current in sizes:
            check_size(self.size, current)
        check_length(length, len(data))
        return data

    def copy_range(self, offset: int, length: int, dst) -> int:
        script = _ranged_read_script(self._source, self._relative_path, offset, length, self.size)
        written, _ = _stream_command(self._session, self._shell_id, script, dst, length)
        check_length(length, written)
        return written


@contextmanager
def open_ranged(source: Source, relative_path: str):
    """The file opened for ranged reads (collectors/ranged.py) for the
    length of the `with` block: one WinRM connection and one shell, asking
    for the larger envelope size and once more with the smaller one when
    the server refuses it. Checked as fetch_file checks it (no junction or
    symlink below the base path, a folder refused), on every read. Behind a
    JEA endpoint, the role's functions do the reading (winrm_jea)."""
    if source.winrm_configuration:
        with winrm_jea.open_ranged(source, relative_path) as remote:
            yield remote
        return
    path = _ps_quote(_remote_path(source, relative_path))
    size_script = (
        _containment_guard(source, relative_path)
        + _require_file(path)
        + f"(Get-Item -LiteralPath {path}).Length"
    )

    def attempt(envelope_size: int):
        session = _session(source, envelope_size)
        protocol = session.protocol
        try:
            shell_id = protocol.open_shell()
            try:
                parts: list[bytes] = []
                _, code, stderr = _command_output(session, shell_id, size_script, parts.append)
                size_text = b"".join(parts).decode("ascii", errors="replace")
                _raise_for_marker(size_text)
                _raise_for_exit(session, code, stderr)
                return session, shell_id, size_text
            except BaseException:
                _close_quietly(protocol.close_shell, shell_id, close_session=False)
                raise
        except BaseException:
            _close_quietly(protocol.transport.close_session)
            raise

    try:
        (session, shell_id, size_text), _ = with_envelope_fallback(source, attempt)
    except PathOutsideBaseError:
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    protocol = session.protocol
    try:
        yield _WinRSRemote(source, relative_path, int(size_text.strip() or 0), session, shell_id)
    except PathOutsideBaseError:
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    finally:
        _close_quietly(protocol.close_shell, shell_id, close_session=False)
        _close_quietly(protocol.transport.close_session)


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
    script = (
        guard
        + _require_file(path)
        + (
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
