import io
import posixpath
import stat as stat_module
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import paramiko

from app.collectors.base import DirEntry
from app.config import get_settings
from app.crypto import decrypt_credential
from app.models import Rule, Source
from app.rules import is_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy"]

# Guards load_host_keys()/save_host_keys() against the shared known_hosts
# file being read and rewritten concurrently by two connections from
# FastAPI's thread pool at once (paramiko's HostKeys I/O isn't itself
# thread-safe) -- see _connect()'s docstring for why this file exists at
# all.
_host_keys_lock = threading.Lock()


def _connect(source: Source) -> paramiko.SSHClient:
    """A fresh connection per call — nothing is kept open across requests,
    matching the project's fetch-on-open model (CLAUDE.md's "Live browsing &
    ephemeral fetch behavior"). credential_ref decrypts to a JSON blob with
    `username` plus either `private_key` or `password` (see docs/source-
    setup.md for what to configure on the source server side).

    Host keys are persisted across connections in a shared known_hosts file
    (ssh_known_hosts_path) rather than trusted fresh every single time.
    AutoAddPolicy on its own only decides what happens for a host paramiko
    has *never* seen at all (trust-on-first-use, same as a real SSH
    client's "accept-new" mode) — paramiko itself, independent of policy,
    already raises BadHostKeyException if a host key it *has* on file
    doesn't match what the server just presented. Without ever loading or
    saving that file, every connection looked "never seen" and silently
    trusted whatever key showed up, every time — this is the fix, not a
    behavior change to AutoAddPolicy itself."""
    creds = decrypt_credential(source.credential_ref)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {"username": creds["username"], "timeout": 10}
    if "private_key" in creds:
        connect_kwargs["pkey"] = paramiko.RSAKey.from_private_key(io.StringIO(creds["private_key"]))
    elif "password" in creds:
        connect_kwargs["password"] = creds["password"]
    else:
        raise ValueError("SSH credential must include private_key or password")

    known_hosts_path = Path(get_settings().ssh_known_hosts_path)
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)

    # Lock only guards the file read/write, not the network round-trip in
    # connect() below -- holding it across a potentially slow SSH handshake
    # would serialize every concurrent connection in the app onto one
    # source at a time, even to unrelated hosts. The narrow gap this
    # leaves (two hosts discovered for the very first time in the same
    # instant could clobber each other's freshly-saved entry) only ever
    # costs a redo of TOFU for the host that lost the race, next time it's
    # connected to -- an already-pinned host's entry was already on disk
    # before either connection started, so it can't be un-pinned this way.
    with _host_keys_lock:
        if known_hosts_path.exists():
            client.load_host_keys(str(known_hosts_path))

    # If the host's key already differs from what's on file, this raises
    # paramiko.ssh_exception.BadHostKeyException -- that's the intended
    # MITM-detection outcome, so it's left uncaught and propagates to the
    # caller like any other connection failure.
    client.connect(source.host, port=source.port or 22, **connect_kwargs)

    with _host_keys_lock:
        client.save_host_keys(str(known_hosts_path))

    return client


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Live directory listing filtered through the rule engine. Directories
    are always listed (never filtered by the rule chain) so the tree stays
    navigable toward deeper matches like `**/*.log` — only files are subject
    to CLAUDE.md's "Rules decide which files ... are visible" filtering. An
    exclude rule targeting a directory still hides everything under it,
    since its files would fail the rule check; the directory itself may
    still appear (possibly empty), which is a UX wrinkle, not a security
    gap."""
    remote_path = (
        posixpath.join(source.base_path, relative_path) if relative_path else source.base_path
    )
    client = _connect(source)
    try:
        sftp = client.open_sftp()
        try:
            entries = []
            for attr in sftp.listdir_attr(remote_path):
                child_path = f"{relative_path}/{attr.filename}" if relative_path else attr.filename
                is_dir = stat_module.S_ISDIR(attr.st_mode or 0)
                if not is_dir and not is_visible(child_path, rules):
                    continue
                entries.append(
                    DirEntry(
                        name=attr.filename,
                        path=child_path,
                        is_dir=is_dir,
                        size=attr.st_size or 0,
                    )
                )
            return entries
        finally:
            sftp.close()
    finally:
        client.close()


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Fetch-on-open into `destination` — always a fresh transfer, the
    caller (scratch.py) never reuses a prior fetch's bytes."""
    remote_path = posixpath.join(source.base_path, relative_path)
    client = _connect(source)
    try:
        sftp = client.open_sftp()
        try:
            sftp.get(remote_path, str(destination))
        finally:
            sftp.close()
    finally:
        client.close()


@contextmanager
def local_copy(source: Source, relative_path: str):
    """Yields a local filesystem path with `relative_path`'s content, for
    callers (api/archive.py) that need to run zipfile/tarfile/gzip against
    it — remote protocols have no choice but to fetch a copy first."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path
