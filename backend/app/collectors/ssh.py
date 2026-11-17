import base64
import hashlib
import io
import os
import posixpath
import stat as stat_module
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
from app.scratch import scratch_temp_dir

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


def host_keys_lock() -> threading.Lock:
    """The lock above, for other code that rewrites known_hosts
    (app/source_pins.py)."""
    return _host_keys_lock


def host_key_name(host: str, port: int | None) -> str:
    """How paramiko names a host in known_hosts."""
    port = port or 22
    return host if port == 22 else f"[{host}]:{port}"


# Keeps NAT and firewalls from dropping a connection during a long transfer.
KEEPALIVE_SECONDS = 30


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

    def __init__(self) -> None:
        # What this connection pinned, for _connect to write to known_hosts.
        self.pinned: list[tuple[str, paramiko.PKey]] = []

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        self.pinned.append((hostname, key))
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


ENCRYPTED_PRIVATE_KEY_DETAIL = (
    "Encrypted SSH private keys aren't supported; provide the key without a passphrase"
)


class PrivateKeyError(ValueError):
    """The saved private key can't be loaded. collectors/errors.py answers it
    with the connector's neutral credential error, never a 500."""


class EncryptedPrivateKeyError(PrivateKeyError):
    """The key is protected by a passphrase."""


def load_private_key(key_text: str) -> paramiko.PKey:
    """Parses a pasted ed25519, ECDSA or RSA private key, in either OpenSSH or
    PEM format. Passphrase-protected keys aren't supported -- there's nowhere
    to store a passphrase, and storing it next to the key would add nothing.
    Saving a source refuses them up front (`is_encrypted_private_key`)."""
    for key_class in _KEY_CLASSES:
        try:
            return key_class.from_private_key(io.StringIO(key_text))
        except paramiko.PasswordRequiredException as exc:
            raise EncryptedPrivateKeyError(ENCRYPTED_PRIVATE_KEY_DETAIL) from exc
        # A malformed key can fail in many ways: try the next key type.
        except Exception:  # noqa: BLE001  # nosec B112
            continue
    raise PrivateKeyError("not a supported SSH private key (expected ed25519, ECDSA or RSA)")


def is_encrypted_private_key(key_text: str) -> bool:
    try:
        load_private_key(key_text)
    except EncryptedPrivateKeyError:
        return True
    except PrivateKeyError:
        return False
    return False


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
    policy = _missing_host_key_policy()
    client.set_missing_host_key_policy(policy)

    read_timeout = get_settings().remote_read_timeout_seconds
    connect_kwargs: dict = {
        "username": creds["username"],
        "timeout": 10,
        # A server that accepts TCP and then goes quiet (#343).
        "banner_timeout": read_timeout,
        "auth_timeout": read_timeout,
        "channel_timeout": read_timeout,
    }
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
    # source at a time, even to unrelated hosts. What was loaded here is
    # never written back: only a key this connection pinned is added, to
    # the file as it is then (_save_pinned_host_keys), so a pin removed
    # meanwhile (app/source_pins.py) stays removed.
    with _host_keys_lock:
        if known_hosts_path.exists():
            client.load_host_keys(str(known_hosts_path))

    # If the host's key already differs from what's on file, this raises
    # paramiko.ssh_exception.BadHostKeyException -- that's the intended
    # MITM-detection outcome, so it's left uncaught and propagates to the
    # caller like any other connection failure.
    client.connect(source.host, port=source.port or 22, **connect_kwargs)
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(KEEPALIVE_SECONDS)

    if isinstance(policy, TrustOnFirstUsePolicy) and policy.pinned:
        _save_pinned_host_keys(known_hosts_path, policy.pinned)

    return client


def _save_pinned_host_keys(path: Path, pinned: list[tuple[str, paramiko.PKey]]) -> None:
    """Adds the keys a connection pinned to known_hosts, reloaded under the
    lock: the rest of the file is kept as it is now, not as the connection
    loaded it."""
    with _host_keys_lock:
        keys = paramiko.HostKeys()
        if path.exists():
            keys.load(str(path))
        for hostname, key in pinned:
            keys.add(hostname, key.get_name(), key)
        keys.save(str(path))
        os.chmod(path, 0o600)  # #193: which hosts are trusted


def _open_sftp(client: paramiko.SSHClient) -> paramiko.SFTPClient:
    """An SFTP session whose reads fail after REMOTE_READ_TIMEOUT_SECONDS of
    silence, instead of blocking the thread forever (#343)."""
    sftp = client.open_sftp()
    sftp.get_channel().settimeout(get_settings().remote_read_timeout_seconds)
    return sftp


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
        sftp = _open_sftp(client)
        try:
            root = sftp.normalize(source.base_path)
            remote_path = _resolve_contained(sftp, source, relative_path)
            entries = []
            for attr in sftp.listdir_attr(remote_path):
                child_path = f"{relative_path}/{attr.filename}" if relative_path else attr.filename
                mode = attr.st_mode or 0
                size = attr.st_size or 0
                mtime = getattr(attr, "st_mtime", None)
                # The folder's real path, so a recursive walk can tell a
                # link that loops back up (#347).
                real_key = posixpath.join(remote_path, attr.filename)
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
                    mtime = getattr(target_attr, "st_mtime", None)
                    real_key = target
                is_dir = stat_module.S_ISDIR(mode)
                if not is_entry_visible(child_path, is_dir, rules, case_insensitive=False):
                    continue
                entries.append(
                    DirEntry(
                        name=attr.filename,
                        path=child_path,
                        is_dir=is_dir,
                        size=size,
                        mtime=float(mtime) if mtime is not None else None,
                        real_key=real_key if is_dir else None,
                    )
                )
            return entries
        finally:
            sftp.close()
    finally:
        client.close()


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Fetch-on-open into `destination` — always a fresh transfer, the
    caller (scratch.py) never reuses a prior fetch's bytes. A file over
    MAX_FETCH_MB is refused before the transfer starts, from its size, and
    one that grows past it while it is copied stops the copy there."""
    limit = collector_base.max_fetch_bytes()
    client = _connect(source)
    try:
        sftp = _open_sftp(client)
        try:
            remote_path = _resolve_contained(sftp, source, relative_path)
            size = sftp.stat(remote_path).st_size or 0
            if size > limit:
                raise collector_base.too_large(size, limit)
            with sftp.open(remote_path, "rb") as remote:
                # Asks for the listed size ahead, as SFTPClient.get does;
                # anything past it is read on demand and still counted.
                remote.prefetch(size)
                collector_base.copy_with_limit(remote, destination, limit)
        finally:
            sftp.close()
    finally:
        client.close()


@contextmanager
def local_copy(source: Source, relative_path: str):
    """Yields a local filesystem path with `relative_path`'s content, for
    callers (api/archive.py) that need to run zipfile/tarfile/gzip against
    it — remote protocols have no choice but to fetch a copy first."""
    with scratch_temp_dir() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path


def read_range(
    source: Source, relative_path: str, offset: int, length: int, head: int = 0
) -> collector_base.RangeRead:
    """Incremental follow (#254): the file's size, its first `head` bytes and
    up to `length` bytes from `offset`, read with an SFTP seek -- nothing before the offset is
    transferred."""
    client = _connect(source)
    try:
        sftp = _open_sftp(client)
        try:
            remote_path = _resolve_contained(sftp, source, relative_path)
            with sftp.open(remote_path, "rb") as f:
                size = f.stat().st_size or 0
                return collector_base.read_range_from(f, size, offset, length, head)
        finally:
            sftp.close()
    finally:
        client.close()
