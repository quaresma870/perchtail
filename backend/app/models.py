from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint


class Protocol(StrEnum):
    ssh = "ssh"
    smb = "smb"
    winrm = "winrm"
    local = "local"
    # Phase 2: for sources not reachable inbound. The Go push-agent dials
    # *out* to this app (solving the firewall/NAT problem) and holds that
    # connection open; the connector then sends live list/fetch commands
    # down it on demand — see collectors/agent.py and app/agent_registry.py.
    # Deliberately not a proactive sync: CLAUDE.md's always-fresh,
    # nothing-persisted rule still applies here, just relayed through the
    # agent's connection instead of a direct SSH/SMB/WinRM dial.
    agent = "agent"


class RuleType(StrEnum):
    include = "include"
    exclude = "exclude"


class PatternKind(StrEnum):
    glob = "glob"
    regex = "regex"


class Customer(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)

    sources: list["Source"] = Relationship(back_populates="customer")
    folders: list["Folder"] = Relationship(back_populates="customer")


class Folder(SQLModel, table=True):
    """Purely organizational grouping of a customer's sources (and other
    folders) into an arbitrarily nested tree, for both browsing and RBAC
    scoping (see auth/rbac.py). A folder always belongs to exactly one
    customer — folders don't span customers — and nests only within that
    same customer (see CLAUDE.md's "Access control" section)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    customer_id: int = Field(foreign_key="customer.id")
    parent_folder_id: int | None = Field(default=None, foreign_key="folder.id")

    customer: Customer = Relationship(back_populates="folders")
    parent_folder: Optional["Folder"] = Relationship(
        back_populates="child_folders",
        sa_relationship_kwargs={"remote_side": "Folder.id"},
    )
    child_folders: list["Folder"] = Relationship(back_populates="parent_folder")
    sources: list["Source"] = Relationship(back_populates="folder")


class Source(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    # Optional — a source can sit directly under its customer with no
    # folder, for simple setups that don't need grouping.
    folder_id: int | None = Field(default=None, foreign_key="folder.id")
    protocol: Protocol
    host: str
    port: int | None = None
    # Encrypted at rest via app.crypto (SSH key, SMB/WinRM password, etc.);
    # null for the local protocol, which needs no credential.
    credential_ref: str | None = None
    base_path: str
    enabled: bool = True
    schedule_cron: str | None = None
    is_system: bool = False
    # Push-agent (protocol=agent) enrollment: only the SHA-256 hash of the
    # enrollment token is stored (same rationale as password/session-token
    # hashing) — the plaintext is shown exactly once, when generated, via
    # POST /sources/{id}/agent-token. Null for every other protocol.
    agent_token_hash: str | None = None
    # Updated whenever the agent's connection is established or sends a
    # response — purely informational for the admin UI ("last seen"); live
    # connection state itself lives in app.agent_registry, not the DB, since
    # it's inherently per-process and shouldn't survive a restart.
    agent_last_seen_at: datetime | None = None
    # Phase 3 full-text search (see app/search_index.py and ROADMAP.md):
    # opt-in, off by default — same "explicit opt-in, not on-by-default"
    # conservatism as the rule engine's zero-rules-matches-nothing default,
    # since indexing stores short line-level snippets at rest, which some
    # customers' logs may be too sensitive for even in that reduced form.
    search_indexing_enabled: bool = False

    customer: Customer | None = Relationship(back_populates="sources")
    folder: Folder | None = Relationship(back_populates="sources")
    rules: list["Rule"] = Relationship(
        back_populates="source",
        sa_relationship_kwargs={"order_by": "Rule.order", "cascade": "all, delete-orphan"},
    )


class Rule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id")
    order: int
    type: RuleType
    pattern: str
    pattern_kind: PatternKind = PatternKind.glob
    notes: str | None = None

    source: Source = Relationship(back_populates="rules")


class SearchIndexState(SQLModel, table=True):
    """Per-file bookkeeping for the Phase 3 full-text search indexer (see
    app/search_index.py) — tracks what's already indexed for a source so a
    sweep only re-reads files that changed. Deliberately separate from the
    actual searchable content, which lives in the `search_index_fts` SQLite
    FTS5 virtual table (raw SQL, not an ORM model — see
    app.db.ensure_search_schema) rather than here, since FTS5 tables aren't
    representable as a SQLModel/SQLAlchemy table class.

    Staleness is tracked by file size alone, not size+mtime: none of the
    connector protocols (ssh/smb/winrm/local/agent) report a file's
    modification time today, only name/path/is_dir/size (see
    collectors/base.py's DirEntry) — so size is the only signal available
    across all of them uniformly. This under-detects the rare case of a
    same-size content change, which is an accepted tradeoff for a lagging,
    approximate secondary index over log files that are typically
    append-only (grow) or rotated (renamed), not edited in place."""

    __tablename__ = "search_index_state"
    __table_args__ = (
        UniqueConstraint("source_id", "file_path", name="uq_search_index_state_source_path"),
    )

    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id")
    file_path: str
    size: int
    indexed_at: datetime


class SystemSetting(SQLModel, table=True):
    """Deployment-wide feature toggles, admin-configurable from Settings ->
    System (see app/api/system_settings.py) rather than only via an env var
    and a redeploy. Key-value rather than dedicated columns so a new toggle
    (e.g. the audit log viewer, once built) slots in without its own
    migration each time. Missing key == default, defined in code
    (app.system_settings.DEFAULTS), not stored — so shipping a new toggle
    doesn't require backfilling every existing deployment's rows."""

    __tablename__ = "system_setting"

    key: str = Field(primary_key=True)
    value: str
