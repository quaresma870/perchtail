"""WinRM sources behind a JEA endpoint (#169).

When a WinRM source names a PowerShell session configuration
(Source.winrm_configuration), the connector speaks PSRP (pypsrp) to that
endpoint instead of running scripts in a WinRS shell. The endpoint is the
reference read-only role in docs/examples/jea: it runs in NoLanguage mode,
so the only thing PerchTail can do is call its functions, by name and with
typed parameters -- no script text is ever sent:

- Get-PerchTailListing -Path      a folder's entries (base64 UTF-8 JSON)
- Get-PerchTailSize -Path         a file's size
- Read-PerchTailBlock -Path -Offset -Length
- Read-PerchTailRange -Path -Offset -Length -HeadLength   ("size|head|data")
- Get-PerchTailRoots              the folders the role allows

The role itself only accepts paths under the roots its administrator
configured, and refuses junctions and links below them; the account needs
neither administrator rights nor a remote shell. Outputs are the same
formats the WinRS connector already parses (collectors/winrm.py)."""

import base64
import json
from contextlib import contextmanager
from pathlib import Path

from pypsrp.powershell import PowerShell, RunspacePool
from pypsrp.wsman import WSMan

from app.collectors import base as collector_base
from app.collectors.base import DirEntry, PathOutsideBaseError
from app.crypto import decrypt_credential
from app.logging_config import get_logger
from app.models import Rule, Source

logger = get_logger(__name__)

_CHUNK_BYTES = 4 * 1024 * 1024


def _wsman(source: Source) -> WSMan:
    from app.collectors import winrm  # shares the endpoint and CA handling

    collector_base.require_credential(source)
    creds = decrypt_credential(source.credential_ref)
    scheme, port = winrm.endpoint(source)
    cert_validation: bool | str = True
    if scheme == "https" and source.tls_ca_pem:
        cert_validation = winrm._ca_bundle_path(source.tls_ca_pem)
    return WSMan(
        source.host,
        port=port,
        username=creds["username"],
        password=creds["password"],
        ssl=scheme == "https",
        auth="ntlm",
        cert_validation=cert_validation,
        path="wsman",
    )


@contextmanager
def _pool(source: Source):
    """One runspace on the source's JEA endpoint, for one operation (a fetch
    reads all its blocks through the same one). Nothing is kept open across
    requests."""
    with RunspacePool(_wsman(source), configuration_name=source.winrm_configuration) as pool:
        yield pool


def _call(pool: RunspacePool, command: str, **parameters) -> str:
    """Runs one of the role's functions and returns its text output. A
    marker the role throws becomes the matching exception; any other error
    is a RuntimeError with PowerShell's message."""
    ps = PowerShell(pool)
    ps.add_cmdlet(command)
    for name, value in parameters.items():
        ps.add_parameter(name, value)
    output = ps.invoke()
    if ps.had_errors:
        text = "; ".join(str(error) for error in ps.streams.error) or "PowerShell error"
        if "PERCHTAIL_OUTSIDE_BASE" in text:
            raise PathOutsideBaseError("path outside the JEA role's roots")
        if "PERCHTAIL_NOT_A_DIRECTORY" in text:
            raise NotADirectoryError("not a directory")
        if "PERCHTAIL_NOT_FOUND" in text:
            raise FileNotFoundError("not found")
        raise RuntimeError(text)
    return "".join(str(item) for item in output if item is not None).strip()


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    from app.collectors import winrm

    path = winrm._remote_path(source, relative_path)
    try:
        with _pool(source) as pool:
            output = _call(pool, "Get-PerchTailListing", Path=path)
    except PathOutsideBaseError:
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    return winrm.parse_listing(output, relative_path, rules)


def list_drives(source: Source) -> list[str]:
    """For the Base path picker: the folders the role allows, which is all
    a JEA source can ever read."""
    with _pool(source) as pool:
        output = _call(pool, "Get-PerchTailRoots")
    if not output:
        return []
    raw = json.loads(base64.b64decode(output).decode("utf-8"))
    return sorted([raw] if isinstance(raw, str) else raw)


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    from app.collectors import winrm

    path = winrm._remote_path(source, relative_path)
    limit = collector_base.max_fetch_bytes()
    written = 0
    try:
        with _pool(source) as pool:
            size = int(_call(pool, "Get-PerchTailSize", Path=path) or 0)
            if size > limit:
                raise collector_base.too_large(size, limit)
            with open(destination, "wb") as dst:
                while written < size:
                    block = base64.b64decode(
                        _call(
                            pool,
                            "Read-PerchTailBlock",
                            Path=path,
                            Offset=written,
                            Length=_CHUNK_BYTES,
                        )
                    )
                    if not block:
                        break  # the file shrank (rotated) while being read
                    dst.write(block)
                    written += len(block)
    except PathOutsideBaseError:
        destination.unlink(missing_ok=True)
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def read_range(
    source: Source, relative_path: str, offset: int, length: int, head: int = 0
) -> collector_base.RangeRead:
    from app.collectors import winrm

    path = winrm._remote_path(source, relative_path)
    try:
        with _pool(source) as pool:
            output = _call(
                pool,
                "Read-PerchTailRange",
                Path=path,
                Offset=max(int(offset), 0),
                Length=max(min(int(length), collector_base.RANGE_MAX_BYTES), 0),
                HeadLength=max(min(int(head), collector_base.RANGE_MAX_HEAD_BYTES), 0),
            )
    except PathOutsideBaseError:
        logger.warning("winrm.path_outside_base", source_id=source.id, path=relative_path)
        raise
    return winrm.parse_range(output)
