from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import JSON, Column
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


class SeverityLevel(StrEnum):
    error = "error"
    warning = "warning"
    info = "info"
    debug = "debug"


# AUTOINCREMENT on every table a RoleGrant (or another row) can point at by
# id: plain SQLite INTEGER PRIMARY KEY reuses the highest id after a delete,
# so a stale grant, index row or alert left behind for a deleted row would
# silently attach to whatever row got that id next.
NO_ID_REUSE = {"sqlite_autoincrement": True}


class Customer(SQLModel, table=True):
    __table_args__ = NO_ID_REUSE

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

    __table_args__ = NO_ID_REUSE

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
    __table_args__ = NO_ID_REUSE

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
    # When the credential was last set (#248), for its age; None when it
    # was set before 0.7 or there is none.
    credential_updated_at: datetime | None = None
    base_path: str
    # Two or more {"label", "path"} entries when the source reads several
    # folders (#161; see app/source_roots.py), else null. base_path then
    # mirrors the first root's path.
    roots: list[dict] | None = Field(default=None, sa_column=Column(JSON))
    # WinRM only (#169): "https" or "http". Null keeps the pre-0.5.0 rule of
    # inferring it from the port (HTTPS on 5986 or no port, HTTP otherwise).
    winrm_scheme: str | None = None
    # WinRM only (#169): the PowerShell session configuration (JEA endpoint)
    # to connect to. Set, the connector speaks PSRP and calls only the
    # reference read-only role's functions (docs/examples/jea); null keeps
    # the WinRS shell, which needs remote-shell rights.
    winrm_configuration: str | None = None
    # PEM CA certificate(s) trusted for this source's TLS listener (WinRM
    # over HTTPS) -- internal PKI or a self-signed certificate, without a
    # server-wide REQUESTS_CA_BUNDLE. Public data, not a secret.
    tls_ca_pem: str | None = None
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
    # The OS the agent says it runs on (Go's runtime.GOOS: "windows",
    # "linux", ...), sent on every connect (#189). Decides whether rules are
    # matched case-insensitively, as for SMB/WinRM. Null until an agent
    # that reports it (0.5.1 or later) has connected.
    agent_os: str | None = None
    # The agent's own version, from the X-PerchTail-Agent-Version header it
    # sends on connect (#215). Null until an agent that reports it (0.6 or
    # later) has connected.
    agent_version: str | None = None
    # Reachability (#276), shared by everyone: "ok" or "failing", when it was
    # last known, and the public reason when failing (never the raw error).
    # Set by the background check and by every real operation on the source
    # (app/source_status.py). Null until first known; agent sources use the
    # live connection instead.
    status: str | None = None
    status_checked_at: datetime | None = None
    status_message: str | None = None
    # File-name index (#304, app/name_index.py): names, sizes and times of the
    # source's rule-visible files, never content. On by default; off here
    # leaves the source out (and drops what was indexed).
    name_index_enabled: bool = True
    name_indexed_at: datetime | None = None
    name_index_files: int | None = None
    # Why the last pass failed or stopped short; None when it went to the end.
    name_index_message: str | None = None

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


class SeverityPattern(SQLModel, table=True):
    """Admin-configurable line-highlighting rule for the Viewer (see
    CLAUDE.md's CodeMirror choice and ROADMAP.md's "Viewer: toward an
    advanced editor" section) — display-only, never affects what's
    readable/fetchable the way `Rule` does. Matching itself runs client-side
    (see frontend/src/lib/severity-highlighting.ts); this table is pure
    config storage + CRUD.

    `source_id` null means a global default pattern; set means a
    per-source override. A source's own patterns fully replace the global
    set when it has any at all (not merged with it) — same "override, not
    merge" simplicity as choosing not to invent new fallback rules, see the
    effective-set resolution in app/api/severity_patterns.py."""

    __tablename__ = "severity_pattern"

    id: int | None = Field(default=None, primary_key=True)
    source_id: int | None = Field(default=None, foreign_key="source.id")
    level: SeverityLevel
    pattern: str
    pattern_kind: PatternKind = PatternKind.glob
    enabled: bool = True
    # Whether a match also tints the whole line, not just the matched
    # substring -- traditionally wanted for error/fatal-style markers, not
    # for a routine info/debug token.
    highlight_line: bool = False
    # Separate from `enabled`: whether this pattern's matches are included
    # when stepping through "next problem" navigation. A pattern can be
    # visually highlighted without cluttering that step-through, or vice
    # versa -- deliberately not reusing `enabled` for both meanings (see
    # ROADMAP.md's open-decision note on this).
    include_in_navigation: bool = True


class FileNameEntry(SQLModel, table=True):
    """One rule-visible file of a source, in the file-name index (#304): its
    path, size and modification time as the last listing saw them. Never any
    content. The `file_name_fts` FTS5 table (trigram tokenizer, see
    app/name_index_store.py) indexes `path` for substring search."""

    __tablename__ = "file_name_entry"
    __table_args__ = (UniqueConstraint("source_id", "path", name="uq_file_name_entry_source_path"),)

    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id", index=True)
    path: str
    size: int
    mtime: float | None = None
    seen_at: datetime


class Alert(SQLModel, table=True):
    """Live alerts (#305, app/alerts.py): a webhook is called when lines
    matching `query` are written to files the alert watches. Each pass reads
    only what was appended to those files since the last pass, the way Tail
    does, so nothing of their content is kept.

    Scope: `source_id`, `folder_id` (and every folder below it) or
    `customer_id`, at most one of them; none means every source the owner
    can view. It's resolved at each pass against the owner's current access,
    so revoking it stops the alert without editing it. Within the scope,
    `file_glob` (default `**/*.log`) picks the files, among those the
    file-name index lists; compressed files and archives are never read.

    `query` is plain text matched anywhere in a line, ignoring case, or a
    regular expression (RE2, linear time) when `regex` is set.

    Delivery (#328): a failed webhook is retried with backoff for up to an
    hour (AlertDelivery); after that it's counted in `failed_deliveries`
    with `last_error`, and the alert moves on."""

    __tablename__ = "alert"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    name: str
    query: str
    regex: bool = False
    source_id: int | None = Field(default=None, foreign_key="source.id")
    folder_id: int | None = Field(default=None, foreign_key="folder.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    file_glob: str = "**/*.log"
    webhook_url: str
    enabled: bool = True
    last_checked_at: datetime | None = None
    last_fired_at: datetime | None = None
    failed_deliveries: int = 0
    last_error: str | None = None
    created_at: datetime


class AlertFileState(SQLModel, table=True):
    """Where an alert has read each file up to: the byte `offset` of the
    next unread line, and a hash of the file's first bytes (`head`) to tell
    a rotated or rewritten file (same path, new content) from one that
    only grew. A file first seen starts at its end."""

    __tablename__ = "alert_file_state"

    alert_id: int = Field(foreign_key="alert.id", primary_key=True)
    source_id: int = Field(foreign_key="source.id", primary_key=True)
    file_path: str = Field(primary_key=True)
    offset: int = 0
    head: str | None = None
    checked_at: datetime | None = None


class AlertDelivery(SQLModel, table=True):
    """A webhook call waiting to be sent or retried (#328). Sent right away;
    when that fails, retried with backoff until it succeeds or is an hour
    old, then dropped and counted on the alert."""

    __tablename__ = "alert_delivery"

    id: int | None = Field(default=None, primary_key=True)
    alert_id: int = Field(foreign_key="alert.id", index=True)
    payload: dict = Field(sa_column=Column("payload", JSON, nullable=False))
    created_at: datetime
    attempts: int = 0
    next_attempt_at: datetime


class UpdateCheckState(SQLModel, table=True):
    """The last result of the release check (#215, app/update_check.py).
    Singleton by convention, like AuditChainState. Keeps the ETag so a
    check that finds nothing new is a 304 and doesn't count against
    GitHub's unauthenticated rate limit."""

    __tablename__ = "update_check_state"

    id: int | None = Field(default=None, primary_key=True)
    etag: str | None = None
    latest_version: str | None = None
    latest_name: str | None = None
    latest_url: str | None = None
    latest_published_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    # The newest version already logged as update_check.available, so it's
    # logged once per release, not once a day.
    notified_version: str | None = None


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


class MonitoringToken(SQLModel, table=True):
    """A single, deployment-wide bearer token gating the detailed health
    endpoint (GET /monitoring/health, see app/api/monitoring.py) — a
    monitoring system like Zabbix can't do an interactive cookie-session
    login, so this is deliberately separate from user auth. Only the
    SHA-256 hash is stored, same "hash at rest, plaintext shown once"
    pattern as Source.agent_token_hash and session tokens; generating a
    new one invalidates whatever token was issued before. Singleton by
    convention (at most one row) rather than a dedicated key-value slot,
    since it's a credential, not a feature toggle -- SystemSetting's
    get_bool/set_bool shape doesn't fit a secret."""

    __tablename__ = "monitoring_token"

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str
    created_at: datetime
