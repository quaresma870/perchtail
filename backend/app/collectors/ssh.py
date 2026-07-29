import io
import posixpath
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

import paramiko

from app.crypto import decrypt_credential
from app.models import Rule, Source
from app.rules import is_visible


@dataclass(frozen=True)
class DirEntry:
    name: str
    path: str
    is_dir: bool
    size: int


def _connect(source: Source) -> paramiko.SSHClient:
    """A fresh connection per call — nothing is kept open across requests,
    matching the project's fetch-on-open model (CLAUDE.md's "Live browsing &
    ephemeral fetch behavior"). credential_ref decrypts to a JSON blob with
    `username` plus either `private_key` or `password` (see docs/source-
    setup.md for what to configure on the source server side)."""
    creds = decrypt_credential(source.credential_ref)

    client = paramiko.SSHClient()
    # Sources are admin-configured, not arbitrary user input, so trust-on-
    # first-use is an acceptable default here; pinning host keys is a
    # possible future hardening, not required for this milestone.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {"username": creds["username"], "timeout": 10}
    if "private_key" in creds:
        connect_kwargs["pkey"] = paramiko.RSAKey.from_private_key(io.StringIO(creds["private_key"]))
    elif "password" in creds:
        connect_kwargs["password"] = creds["password"]
    else:
        raise ValueError("SSH credential must include private_key or password")

    client.connect(source.host, port=source.port or 22, **connect_kwargs)
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
