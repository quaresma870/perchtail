"""The one mapping from a source's protocol to the collector module that
serves it. Every caller (browse/open/download in api/archive.py, the
connection check in api/sources.py, the search indexer) uses this, so a
protocol can't be wired into one code path and silently missing from
another -- which is how agent sources passed /check but answered 501 on
every browse in v0.4.0 (#165)."""

from types import ModuleType

from app.collectors import agent, local, smb, ssh, winrm
from app.models import Protocol

CONNECTORS: dict[Protocol, ModuleType] = {
    Protocol.ssh: ssh,
    Protocol.smb: smb,
    Protocol.winrm: winrm,
    Protocol.local: local,
    Protocol.agent: agent,
}
