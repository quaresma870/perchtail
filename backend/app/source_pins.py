"""What a source leaves next to the database, removed with it or when it is
pointed at another host, port or CA: its SSH host key in
SSH_KNOWN_HOSTS_PATH (app/collectors/ssh.py) and its WinRM CA certificate
file under DATA_DIR/winrm_ca (app/collectors/winrm.py).

Both are shared by every source that points at the same place: a host key
is pinned per host and port, and a CA file is named after the certificate's
hash. One is removed only when no other source still uses it, so deleting
one source never makes another trust a new key, or lose its CA, unasked.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import paramiko
from sqlmodel import Session, select

from app.collectors import ssh as ssh_collector
from app.config import get_settings
from app.logging_config import get_logger
from app.models import Protocol, Source

logger = get_logger(__name__)


@dataclass(frozen=True)
class SourcePins:
    """What a source used, kept from before it was deleted or changed."""

    protocol: Protocol
    host: str
    port: int | None
    tls_ca_pem: str | None

    @classmethod
    def of(cls, source: Source) -> "SourcePins":
        return cls(source.protocol, source.host, source.port, source.tls_ca_pem)


_host_key_name = ssh_collector.host_key_name


def _ca_file(pem: str) -> Path:
    name = f"{hashlib.sha256(pem.encode()).hexdigest()}.pem"
    return Path(get_settings().data_dir) / "winrm_ca" / name


def remove_unused_pins(session: Session, pins: SourcePins) -> list[str]:
    """Removes the host key and CA file in `pins` when no source uses them
    (a changed source counts with its new settings); call it after the
    delete or change is committed. Returns what
    was removed, as `host_key` / `ca_certificate`, for the caller to audit.
    A file that can't be changed is logged and left."""
    removed: list[str] = []
    others = session.exec(select(Source)).all()

    if pins.protocol == Protocol.ssh:
        name = _host_key_name(pins.host, pins.port)
        if not any(
            s.protocol == Protocol.ssh and _host_key_name(s.host, s.port) == name for s in others
        ):
            if _remove_host_key(name):
                removed.append("host_key")

    if pins.tls_ca_pem and not any(s.tls_ca_pem == pins.tls_ca_pem for s in others):
        path = _ca_file(pins.tls_ca_pem)
        try:
            if path.exists():
                path.unlink()
                removed.append("ca_certificate")
        except OSError as exc:
            logger.warning("source.ca_file_not_removed", path=str(path), error=str(exc))
    return removed


def _remove_host_key(name: str) -> bool:
    path = Path(get_settings().ssh_known_hosts_path)
    with ssh_collector.host_keys_lock():
        if not path.exists():
            return False
        try:
            keys = paramiko.HostKeys(str(path))
            if name not in keys:
                return False
            # One entry per key type (ed25519, ecdsa, rsa...): all of them go.
            while name in keys:
                del keys[name]
            keys.save(str(path))
            path.chmod(0o600)
        except (OSError, paramiko.SSHException) as exc:
            logger.warning("source.host_key_not_removed", host=name, error=str(exc))
            return False
    logger.info("source.host_key_removed", host=name)
    return True
