import base64
import hashlib
import io
import os
import posixpath
import stat as stat_module
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import paramiko

from app.collectors import base as collector_base
from app.collectors.base import DirEntry, PathOutsideBaseError
from app.config import get_settings
from app.crypto import decrypt_credential
from app.logging_config import get_logger
from app.models import Rule, Source
from app.rules import is_entry_visible

__all__ = [
    "DirEntry",
    "PathOutsideBaseError",
    "TrustOnFirstUsePolicy",
    "fetch_file",
    "fingerprint",
    "load_private_key",
    "list_directory",
    "local_copy",
]

logger = get_logger(__name__)

# Guards load_host_keys()/save_host_keys() against the shared known_hosts
# file being read and rewritten concurrently by two connections from
# FastAPI's thread pool at once (paramiko's HostKeys I/O isn't itself
# thread-safe) -- see _connect()'s docstring for why this file exists at
# all.
_host_keys_lock = threading.Lock()


def fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-style SHA256 fingerprint, the form `ssh-keygen -lf` prints,
    so an operator can compare a logged pin against the server's own key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class TrustOnFirstUsePolicy(paramiko.MissingHostKeyPolicy):
    """Pins a host's key the first time PerchTail connects to it (OpenSSH's
    `StrictHostKeyChecking=accept-new`). Only ever consulted for a host with
    no key on file at all: a host whose pinned key changes fails with
    paramiko's BadHostKeyException before any policy runs. Unlike
    paramiko.AutoAddPolicy, every pin is logged with its fingerprint, so
    there's a record of which key was trusted and when. Set
    SSH_HOST_KEY_POLICY=strict to refuse unknown hosts instead."""

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        logger.warning(
            "ssh.host_key_pinned",
            host=hostname,
            key_type=key.get_name(),
            fingerprint=fingerprint(key),
        )


# Tried in order; RSA last since it's the least likely to be what someone
# generating a new key today (ssh-keygen's default is ed25519) pastes in.
_KEY_CLASSES: tuple[type[paramiko.PKey], ...] = (
    paramiko.Ed25519Key,
    paramiko.ECDSAKey,
    paramiko.RSAKey,
)


def load_private_key(key_text: str) -> paramiko.PKey:
    """Parses a pasted ed25519, ECDSA or RSA private key, in either OpenSSH or
    PEM format. Passphrase-protected keys aren't supported -- there's nowhere
    to store a passphrase, and storing it next to the key would add nothing."""
    for key_class in _KEY_CLASSES:
        try:
            return key_class.from_private_key(io.StringIO(key_text))
        except paramiko.PasswordRequiredException as exc:
            raise ValueError(
                "SSH private key is protected by a passphrase; "
                "use a key without one (see docs/source-setup.md)"
            ) from exc
        except (paramiko.SSHException, ValueError):
            continue
    raise ValueError("not a supported SSH private key (expected ed25519, ECDSA or RSA)")


def _missing_host_key_policy() -> paramiko.MissingHostKeyPolicy:
    if get_settings().ssh_host_key_policy == "strict":
        return paramiko.RejectPolicy()
    return TrustOnFirstUsePolicy()


def _connect(source: Source) -> paramiko.SSHClient:
    """A fresh connection per call — nothing is kept open across requests,
    matching the project's fetch-on-open model (CLAUDE.md's "Live browsing &
    ephemeral fetch behavior"). credential_ref decrypts to a JSON blob with
    `username` plus either `private_key` or `password` (see docs/source-
    setup.md for what to configure on the source server side).

    Host keys are persisted across connections in a shared known_hosts file
    (ssh_known_hosts_path) rather than trusted fresh every single time.
    The missing-host-key policy (TrustOnFirstUsePolicy, or RejectPolicy in
    strict mode) only decides what happens for a host paramiko has *never*
    seen at all — paramiko itself, independent of policy,
    already raises BadHostKeyException if a host key it *has* on file
    doesn't match what the server just presented. Without ever loading or
    saving that file, every connection looked "never seen" and silently
    trusted whatever key showed up, every time."""
    collector_base.require_credential(source)
    creds = decrypt_credential(source.credential_ref)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_missing_host_key_policy())

    connect_kwargs: dict = {"username": creds["username"], "timeout": 10}
    if "private_key" in creds:
        connect_kwargs["pkey"] = load_private_key(creds["private_key"])
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
        if known_hosts_path.exists():
            os.chmod(known_hosts_path, 0o600)  # #193: which hosts are trusted

    return client


def _is_within(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root if root == "/" else root + "/")


def _resolve_contained(sftp: paramiko.SFTPClient, source: Source, relative_path: str) -> str:
    """Resolves `relative_path` on the server (SSH_FXP_REALPATH, which
    follows symlinks) and returns the real path, refusing anything that
    ends up outside the resolved base path. Without this, a symlink inside
    the base path (`escape -> /etc`) would let anyone with `view` on the
    source read whatever the SSH account can read -- the base path and the
    rules only ever saw the link's name. The caller must then use the
    returned real path, not the joined one, so the link can't be swapped
    between this check and the read."""
    root = sftp.normalize(source.base_path)
    joined = posixpath.join(source.base_path, relative_path) if relative_path else source.base_path
    try:
        real = sftp.normalize(joined)
    except OSError as exc:
        raise FileNotFoundError(relative_path) from exc
    if not _is_within(real, root):
        logger.warning(
            "ssh.path_outside_base",
            source_id=source.id,
            path=relative_path,
        )
        raise PathOutsideBaseError(relative_path)
    return real


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Live directory listing filtered through the rule engine. Directories
    are always listed (never filtered by the rule chain) so the tree stays
    navigable toward deeper matches like `**/*.log` — only files are subject
    to CLAUDE.md's "Rules decide which files ... are visible" filtering.

    Symlinks are resolved on the server: one that points outside the base
    path is left out of the listing entirely, and one that points inside it
    is shown as whatever it points to (file or directory)."""
    client = _connect(source)
    try:
        sftp = client.open_sftp()
        try:
            root = sftp.normalize(source.base_path)
            remote_path = _resolve_contained(sftp, source, relative_path)
            entries = []
            for attr in sftp.listdir_attr(remote_path):
                child_path = f"{relative_path}/{attr.filename}" if relative_path else attr.filename
                mode = attr.st_mode or 0
                size = attr.st_size or 0
                if stat_module.S_ISLNK(mode):
                    try:
                        target = sftp.normalize(posixpath.join(remote_path, attr.filename))
                        if not _is_within(target, root):
                            continue
                        target_attr = sftp.stat(target)
                    except OSError:
                        continue  # dangling link: nothing to show
                    mode = target_attr.st_mode or 0
                    size = target_attr.st_size or 0
                is_dir = stat_module.S_ISDIR(mode)
                if not is_entry_visible(child_path, is_dir, rules, case_insensitive=False):
                    continue
                entries.append(
                    DirEntry(name=attr.filename, path=child_path, is_dir=is_dir, size=size)
                )
            return entries
        finally:
            sftp.close()
    finally:
        client.close()


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Fetch-on-open into `destination` — always a fresh transfer, the
    caller (scratch.py) never reuses a prior fetch's bytes."""
    limit = collector_base.max_fetch_bytes()
    client = _connect(source)
    try:
        sftp = client.open_sftp()
        try:
            remote_path = _resolve_contained(sftp, source, relative_path)
            size = sftp.stat(remote_path).st_size or 0
            if size > limit:
                raise collector_base.too_large(size, limit)
            sftp.get(remote_path, str(destination))
            # The file may have grown between stat() and the transfer.
            if destination.stat().st_size > limit:
                destination.unlink(missing_ok=True)
                raise collector_base.too_large(size, limit)
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
