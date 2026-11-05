"""The one mapping from a source's protocol to the collector module that
serves it. Every caller (browse/open/download in api/archive.py, the
connection check in api/sources.py, the search indexer) uses this, so a
protocol can't be wired into one code path and silently missing from
another -- which is how agent sources passed /check but answered 501 on
every browse in v0.4.0 (#165)."""

from app.collectors import agent, local, smb, ssh, winrm
from app.collectors.errors import (
    TranslatingConnector,
    translate_agent,
    translate_local,
    translate_smb,
    translate_ssh,
    translate_winrm,
)
from app.models import Protocol

# Each connector raises the SourceError types from collectors/base.py, so
# callers map them to HTTP statuses in one place (app/api/errors.py).
CONNECTORS: dict[Protocol, TranslatingConnector] = {
    Protocol.ssh: TranslatingConnector(ssh, translate_ssh),
    Protocol.smb: TranslatingConnector(smb, translate_smb),
    Protocol.winrm: TranslatingConnector(winrm, translate_winrm),
    Protocol.local: TranslatingConnector(local, translate_local),
    Protocol.agent: TranslatingConnector(agent, translate_agent),
}
