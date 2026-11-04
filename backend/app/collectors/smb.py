import tempfile
from contextlib import contextmanager
from pathlib import Path

import smbclient

from app.collectors import base as collector_base
from app.collectors.base import DirEntry, PathOutsideBaseError
from app.crypto import decrypt_credential
from app.logging_config import get_logger
from app.models import Rule, Source
from app.rules import is_entry_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy"]

logger = get_logger(__name__)


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


# FILE_ATTRIBUTE_REPARSE_POINT (MS-FSCC 2.6): junctions, directory and file
# symlinks, mount points. Windows resolves a junction on the server side,
# so one inside the share leads anywhere the account can read; they are
# never listed or followed.
_REPARSE_POINT = 0x400


@contextmanager
def _connection(source: Source):
    """Connect kwargs bound to a connection cache of this request alone.

    smbclient's default pool is process-wide and keyed by server and
    username: a source with the wrong password used to ride on another
    source's authenticated session to the same host. A private cache means
    every request authenticates with its own credential, and is closed when
    the request is done."""
    cache: dict = {}
    kwargs = {**_connect_kwargs(source), "connection_cache": cache}
    try:
        smbclient.register_session(source.host, **kwargs)
        yield kwargs
    finally:
        smbclient.reset_connection_cache(fail_on_error=False, connection_cache=cache)


def _is_reparse_point(attributes: int) -> bool:
    return bool(attributes & _REPARSE_POINT)


def _require_no_reparse_points(source: Source, relative_path: str, kwargs: dict) -> None:
    """Refuses a path that goes through a junction or symlink anywhere below
    the base path (lstat does not follow the reparse point it is asked
    about). The base path itself is the operator's choice and not checked."""
    parts = [p for p in relative_path.split("/") if p]
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        attributes = smbclient.lstat(_unc_path(source, prefix), **kwargs).st_file_attributes
        if _is_reparse_point(attributes):
            logger.warning("smb.path_outside_base", source_id=source.id, path=prefix)
            raise PathOutsideBaseError(relative_path)


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Live directory listing filtered through the rule engine. Junctions
    and symlinks are left out, without following them: one that can't be
    redirected used to fail the whole listing."""
    with _connection(source) as kwargs:
        _require_no_reparse_points(source, relative_path, kwargs)
        directory = _unc_path(source, relative_path)
        entries = []
        for info in smbclient.scandir(directory, **kwargs):
            # Checked before is_dir(), which would try to follow the link.
            if _is_reparse_point(info.smb_info.file_attributes):
                logger.info(
                    "smb.reparse_point_skipped",
                    source_id=source.id,
                    path=f"{relative_path}/{info.name}" if relative_path else info.name,
                )
                continue
            child_path = f"{relative_path}/{info.name}" if relative_path else info.name
            is_dir = info.is_dir()
            if not is_entry_visible(child_path, is_dir, rules, case_insensitive=True):
                continue
            # info.smb_info.end_of_file (not info.stat().st_size): it comes
            # straight off the listing, and SMBDirEntry.stat() would open a
            # session of its own without this request's credentials.
            size = 0 if is_dir else info.smb_info.end_of_file
            entries.append(DirEntry(name=info.name, path=child_path, is_dir=is_dir, size=size))
        return entries


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Fetch-on-open into `destination` — always a fresh transfer, never
    reused."""
    with _connection(source) as kwargs:
        _require_no_reparse_points(source, relative_path, kwargs)
        remote_path = _unc_path(source, relative_path)
        limit = collector_base.max_fetch_bytes()
        # Refuse before transferring anything when the size is already over
        # the cap (#167) -- copy_with_limit stays as the backstop for a file
        # that grows while it's being read.
        size = smbclient.stat(remote_path, **kwargs).st_size
        if size > limit:
            raise collector_base.too_large(size, limit)
        # share_access="rwd": a read-only open that tolerates other readers,
        # writers and deleters (.NET FileShare.ReadWrite | Delete), so the
        # live log a service has open for writing can be read (#166).
        with smbclient.open_file(remote_path, mode="rb", share_access="rwd", **kwargs) as src:
            collector_base.copy_with_limit(src, destination, limit)


@contextmanager
def local_copy(source: Source, relative_path: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path
