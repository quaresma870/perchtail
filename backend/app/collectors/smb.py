import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import smbclient

from app.collectors.base import DirEntry
from app.crypto import decrypt_credential
from app.models import Rule, Source
from app.rules import is_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy"]


def _connect_kwargs(source: Source) -> dict:
    """Shared connection kwargs for both register_session() and every
    scandir()/open_file() call below. Both matter, not just
    register_session(): smbclient's own scandir()/open_file() resolve
    their *own* session via get_smb_tree(path, port=445, ...) -- port
    defaults to 445 there too, independent of whatever was passed to
    register_session() -- so a source on a non-default port (like this
    project's own e2e test server) would silently look up a session for
    445 instead of reusing the one actually registered, falling back to an
    unauthenticated connection attempt on the wrong port entirely. Passing
    the same port (and credentials, as a defense-in-depth match against
    the same cache key) to every call, not just register_session(), is
    what actually keeps them hitting the same cached session.

    auth_protocol is pinned to NTLM rather than left at smbclient's default
    of "negotiate" (which tries Kerberos first): credential_ref only ever
    decrypts to a bare username/password (see app/crypto.py), with no
    realm/domain/KDC anywhere in the Source model, so this app has no way
    to actually supply Kerberos credentials in the first place. Left at
    "negotiate", a client environment with no Kerberos configuration at
    all (no /etc/krb5.conf) can fail the whole SPNEGO negotiation outright
    -- pyspnego.exceptions.BadMechanismError ("unable to negotiate common
    mechanism") -- instead of ever falling back to the NTLM this app
    actually authenticates with."""
    creds = decrypt_credential(source.credential_ref)
    return {
        "username": creds["username"],
        "password": creds["password"],
        "port": source.port or 445,
        "auth_protocol": "ntlm",
    }


def _unc_path(source: Source, relative_path: str = "") -> str:
    """source.base_path is the share name (optionally with a sub-path), e.g.
    "AppLogs" or "AppLogs\\nested" — see docs/source-setup.md for how the
    share itself is set up on the Windows side."""
    base = source.base_path.strip("\\/")
    tail_parts = [p for p in (base, relative_path.replace("/", "\\")) if p]
    tail = "\\".join(tail_parts)
    return f"\\\\{source.host}\\{tail}" if tail else f"\\\\{source.host}"


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Live directory listing filtered through the rule engine. Directories
    are always listed (never filtered) so the tree stays navigable toward
    deeper matches like `**/*.log` — only files are subject to the rule
    chain (see collectors/ssh.py's list_directory for the same rationale)."""
    connect_kwargs = _connect_kwargs(source)
    smbclient.register_session(source.host, **connect_kwargs)
    directory = _unc_path(source, relative_path)
    entries = []
    for info in smbclient.scandir(directory, **connect_kwargs):
        child_path = f"{relative_path}/{info.name}" if relative_path else info.name
        is_dir = info.is_dir()
        if not is_dir and not is_visible(child_path, rules):
            continue
        # info.smb_info.end_of_file (not info.stat().st_size) deliberately --
        # smb_info comes straight off the directory listing this project
        # already paid for in scandir(), with no further SMB round trip.
        # info.stat() looks tempting but is a trap: SMBDirEntry.stat()
        # forwards only connection_cache to a fresh lstat()/stat() call, not
        # the port/username/auth_protocol this project passes everywhere
        # else -- get_smb_tree() then falls back to its own port=445 default
        # and empty credentials, landing on a different cache key than the
        # one register_session() populated and failing SPNEGO negotiation
        # outright (spnego.exceptions.BadMechanismError) since there's no
        # Kerberos ticket cache either. See _connect_kwargs's docstring for
        # the sibling bugs this same smbclient behavior caused elsewhere.
        size = 0 if is_dir else info.smb_info.end_of_file
        entries.append(DirEntry(name=info.name, path=child_path, is_dir=is_dir, size=size))
    return entries


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Fetch-on-open into `destination` — always a fresh transfer, never
    reused."""
    connect_kwargs = _connect_kwargs(source)
    smbclient.register_session(source.host, **connect_kwargs)
    remote_path = _unc_path(source, relative_path)
    with (
        smbclient.open_file(remote_path, mode="rb", **connect_kwargs) as src,
        open(destination, "wb") as dst,
    ):
        shutil.copyfileobj(src, dst)


@contextmanager
def local_copy(source: Source, relative_path: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path
