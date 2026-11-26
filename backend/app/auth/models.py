from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, Enum
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.timeutils import utcnow


class ScopeType(StrEnum):
    customer = "customer"
    folder = "folder"
    source = "source"


class Capability(StrEnum):
    view = "view"
    download = "download"
    manage_rules = "manage_rules"
    run_now = "run_now"
    # Changing what already exists in a scope: editing, moving and deleting
    # its sources, moving and deleting its folders, a source's agent token,
    # saved credential and agent upgrades -- and, with create_source, adding
    # a source or folder there. Separate from manage_rules, which only
    # governs the visibility rules.
    manage_source = "manage_source"


class GlobalCapability(StrEnum):
    manage_users = "manage_users"
    manage_roles = "manage_roles"
    manage_sso = "manage_sso"
    create_source = "create_source"
    # Deployment-wide feature toggles (SystemSetting) -- distinct from the
    # other capabilities above, which all gate *managing something scoped*
    # (users, roles, SSO config, sources). This one gates flipping a switch
    # that changes what every user in the deployment sees.
    manage_system_settings = "manage_system_settings"
    # Read access to AuditLog (app/api/audit.py). Deliberately its own global
    # capability rather than folded into the customer/folder/source grant
    # tree: audit visibility is a global concern (who did what, anywhere in
    # the deployment), not something scoped to what a role can browse. Never
    # implied by any other capability above -- a role needs this explicitly,
    # same as a super-admin needs no capability at all to see everything.
    view_audit_log = "view_audit_log"
    # Creating or editing an alert. An alert's webhook sends matching log
    # content to an outside URL, an export channel, so it isn't implied by
    # view access to the sources it watches.
    manage_alerts = "manage_alerts"
    # Generating the access report (app/access_report.py): who holds what,
    # where. Read-only, so a reviewer can review access without being able
    # to change it; not implied by manage_users or manage_roles.
    view_access_report = "view_access_report"


class AuthProviderType(StrEnum):
    local = "local"
    oidc = "oidc"
    saml = "saml"


class SSOProtocol(StrEnum):
    oidc = "oidc"
    saml = "saml"


class Role(SQLModel, table=True):
    # See app.models.NO_ID_REUSE -- RoleGrant and SSOGroupRoleMapping point
    # at roles by id.
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    is_builtin: bool = False
    is_super_admin: bool = False
    global_capabilities: list[GlobalCapability] = Field(
        default_factory=list, sa_column=Column(JSON)
    )

    grants: list["RoleGrant"] = Relationship(
        back_populates="role", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    users: list["User"] = Relationship(back_populates="role")


class RoleGrant(SQLModel, table=True):
    # One grant per role and scope: resolution reads a single grant there.
    __table_args__ = (
        UniqueConstraint("role_id", "scope_type", "scope_id", name="uq_rolegrant_scope"),
    )

    id: int | None = Field(default=None, primary_key=True)
    role_id: int = Field(foreign_key="role.id")
    # scope_id points at either customer.id or source.id depending on scope_type,
    # so it can't carry a single FK constraint — validated in auth/rbac.py (M2).
    scope_type: ScopeType = Field(sa_type=Enum(ScopeType, native_enum=False))
    scope_id: int
    capabilities: list[Capability] = Field(default_factory=list, sa_column=Column(JSON))

    role: Role = Relationship(back_populates="grants")


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # Login identifier — an email address or a plain username depending on
    # the auth provider; SSO-provisioned accounts populate this from the
    # IdP's claim.
    username: str = Field(unique=True, index=True)
    password_hash: str | None = None
    # My profile (#243). Optional; set by the user (or an admin) for local
    # accounts, and from the IdP's claims on each sign-in for SSO accounts.
    # The email is stored lowercased and is unique (app/profile.py).
    display_name: str | None = None
    email: str | None = Field(default=None, unique=True, index=True)
    # Display preferences that follow the user (#273, app/preferences.py):
    # explorer sort order, connections list/tree view. Null = defaults.
    preferences: dict | None = Field(default=None, sa_column=Column(JSON))
    role_id: int = Field(foreign_key="role.id")
    active: bool = True
    auth_provider: AuthProviderType = Field(
        default=AuthProviderType.local, sa_type=Enum(AuthProviderType, native_enum=False)
    )
    # An SSO account is identified by (external_issuer, external_id): the
    # IdP's `iss` and `sub` claims. `sub` is only unique within one issuer,
    # so matching on it alone would let a different IdP (after the provider's
    # issuer is changed) sign in as whoever had the same `sub` before.
    external_issuer: str | None = Field(default=None, index=True)
    external_id: str | None = None
    last_login_at: datetime | None = None
    # The IdP group whose SSOGroupRoleMapping gave this SSO user their role
    # at their last sign-in (auth/providers/oidc.py), for the access report
    # (app/access_report.py). Only that one group name is kept, never the
    # token's other groups. Null when no mapping matched, once an admin
    # changes the role by hand (api/users.py), and for local accounts.
    sso_role_group: str | None = None
    # Set on admin-created local accounts (see CLAUDE.md's Security notes);
    # cleared once the user picks their own password.
    must_change_password: bool = False
    # When the local password was last set (#245), for the optional expiry
    # under Settings -> Sign-in security. Null for SSO accounts.
    password_changed_at: datetime | None = None
    # Optional TOTP second factor for local accounts (docs/design-decisions.md,
    # "Authentication and SSO") -- SSO accounts delegate MFA to the IdP instead, so
    # this is only ever meaningful for auth_provider=local. mfa_enabled is
    # False both before enrollment starts and while a pending secret hasn't
    # been confirmed yet (see auth/mfa.py's start_enrollment/
    # confirm_enrollment) -- a half-finished enrollment never gates login.
    mfa_enabled: bool = False
    # Encrypted at rest via app.crypto (same Fernet primitive as
    # Source.credential_ref) -- unlike a password hash, this has to be
    # decryptable, since verifying a live code means feeding the secret back
    # into the TOTP algorithm.
    mfa_secret_encrypted: str | None = None
    mfa_enrolled_at: datetime | None = None
    # Anti-replay: the TOTP time-step counter last accepted for this user.
    # pyotp's own verify() only checks a code's validity within a window, not
    # whether it's been used before -- without tracking this, a single
    # observed code (shoulder-surfed, logged by a proxy) could be replayed
    # again within its ~90s acceptance window. A login/confirm is only
    # accepted if its counter is strictly greater than this value (see
    # auth/mfa.py's _consume_totp_code), mirroring MfaBackupCode.used_at's
    # single-use guarantee for the TOTP path instead of just the backup-code
    # path.
    mfa_last_used_step: int | None = None

    role: Role = Relationship(back_populates="users")


class MfaBackupCode(SQLModel, table=True):
    """One-time recovery codes issued alongside TOTP enrollment (see
    auth/mfa.py's confirm_enrollment) -- lets a user sign in if they lose
    their authenticator device. Only the argon2 hash is stored, same
    rationale as User.password_hash: unlike the TOTP secret above, a backup
    code never needs to be read back in plaintext, only compared against on
    use, so a one-way hash is the tighter choice here."""

    __tablename__ = "mfa_backup_code"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    code_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    # Consumed codes are kept (not deleted) as a record of use rather than
    # silently vanishing -- null means still usable.
    used_at: datetime | None = None


class PasswordHistory(SQLModel, table=True):
    """A local account's previous password hashes (#245), kept only while
    the "don't reuse the last N passwords" rule is on, and only as many as
    it needs (app/password_policy.py). argon2id hashes, never the
    passwords."""

    __tablename__ = "password_history"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class PasswordResetToken(SQLModel, table=True):
    """A pending password-reset link (#244, app/auth/password_reset.py).
    Only the SHA-256 of the token is stored, never the token. It works once
    (the row is deleted when it's used, and when a new link is asked for),
    until expires_at, and only while the account's password hash and email
    are still the ones it was issued for: each fingerprint is the SHA-256 of
    that value at the time of the request."""

    __tablename__ = "password_reset_token"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    token_hash: str = Field(unique=True, index=True)
    password_fingerprint: str
    email_fingerprint: str
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime


class SSOProviderConfig(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    protocol: SSOProtocol = Field(sa_type=Enum(SSOProtocol, native_enum=False))
    name: str
    # Encrypted at rest via app.crypto (client id/secret, or SAML metadata).
    config: str
    enabled: bool = False


class SSOGroupRoleMapping(SQLModel, table=True):
    """Maps an IdP group name to a Role, applied automatically on every SSO
    login (see auth/providers/oidc.py's resolve_group_mapped_role_id) —
    CLAUDE.md's phase-2 "auto-mapping IdP group claims to roles" automation.
    Deliberately global, not scoped to a specific SSOProviderConfig: v1 only
    ever has one active provider at a time (see api/sso.py's
    _assert_single_enabled), so there's nothing to disambiguate yet.

    `order` gives mappings the exact same "evaluated in order, last match
    wins" semantics as Rule (see CLAUDE.md's rule-matching section) —
    reusing a mental model this project's admins already know, rather than
    inventing a new one (e.g. "most privileged role wins") for what's
    otherwise the same kind of ordered-precedence problem. A user whose ID
    token's group claim contains more than one mapped group gets whichever
    mapping is evaluated last.

    Applied on every login, not just first provisioning: if a mapping still
    matches, it overwrites User.role_id each time, keeping the user's role
    in sync with their current IdP group membership. This means an admin's
    manual role change made directly in PerchTail doesn't stick past that
    user's next SSO login if their groups still match a configured mapping
    — the IdP is treated as the source of truth once a mapping exists for
    it, same spirit as an IdP-driven SSO relationship generally. Deleting
    the mapping (or removing the user from the group) stops the resync."""

    __tablename__ = "sso_group_role_mapping"

    id: int | None = Field(default=None, primary_key=True)
    order: int
    group_name: str
    role_id: int = Field(foreign_key="role.id")


class AuthSession(SQLModel, table=True):
    """Server-side session backing the login cookie. Only the SHA-256 hash of
    the token is stored — same rationale as password hashing — so a DB leak
    doesn't directly hand out valid sessions. Chosen over a stateless JWT so
    deactivating a user or logging out actually revokes access immediately,
    without a separate revocation list."""

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    last_seen_at: datetime | None = None
    # Captured at login (local and SSO) straight from the request's own
    # User-Agent header -- not IP address, deliberately: a real client IP
    # behind a reverse proxy means trusting X-Forwarded-For, the same trust
    # question app/config.py's public_base_url sidesteps entirely by not
    # deriving anything from proxy headers. User-Agent has no such
    # trust-boundary issue and is still the main signal the session
    # management UI needs to answer "is this me, or someone else."
    user_agent: str | None = None


class AuditLog(SQLModel, table=True):
    # AUTOINCREMENT (see app.models.NO_ID_REUSE): an entry's id is never
    # handed out again after the purge deletes it, so an id always names one
    # entry, and the purge's own `audit.purge` entry always lands above the
    # rows it deleted.
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="user.id")
    action: str
    target_type: str | None = None
    target_id: int | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    event_metadata: dict | None = Field(default=None, sa_column=Column("metadata", JSON))
    # Tamper-evidence hash chain (see app/audit_hash_chain.py) -- row_hash
    # covers this row's own fields plus prev_hash, so altering or deleting a
    # row anywhere breaks every hash after it. Both nullable: a row written
    # before this feature existed has neither until
    # audit_hash_chain.backfill_chain_if_needed runs once at startup.
    prev_hash: str | None = None
    row_hash: str | None = None
    # Where the action came from (app/request_context.py): the client's
    # address, its User-Agent (truncated), and a short reference to the
    # session (a prefix of the token's SHA-256). Empty for entries written
    # by background jobs, the CLI, or before 0.7.0.
    client_ip: str | None = None
    user_agent: str | None = None
    session_id: str | None = None
    # Which fields row_hash covers (app/audit_hash_chain.py): null for rows
    # written before 0.7.0 (format 1), 2 for rows that also cover the three
    # fields above. Verification hashes each row by its own format.
    hash_format: int | None = None


class AuditChainState(SQLModel, table=True):
    """Singleton bookkeeping row for the AuditLog hash chain -- two
    independent concerns share it: (1) the chain anchor (anchor_row_id/
    anchor_hash), the newest row purged under retention so far, so
    app.audit_purge.run_audit_purge_sweep deleting old rows doesn't look
    like a tamper break to verification -- the first surviving row is
    expected to chain from this hash, not from None; and (2) the result of
    the most recent app.audit_integrity.verify_chain() pass, read by the
    Audit Log page's integrity banner. Singleton by convention, same as
    MonitoringToken -- there is exactly one chain, so exactly one state
    row."""

    __tablename__ = "audit_chain_state"

    id: int | None = Field(default=None, primary_key=True)
    anchor_row_id: int | None = None
    anchor_hash: str | None = None
    last_checked_at: datetime | None = None
    # "unknown" until the first verify_chain() call ever runs -- with this
    # feature's very long default check interval (see
    # Settings.audit_integrity_check_interval_days), that could otherwise be
    # up to a year after upgrade with nothing to show on the banner.
    status: str = "unknown"
    broken_row_id: int | None = None
    # Known breaks from the #213 race (#217, see app.audit_integrity). The
    # cutoff is the newest row written before upgrading to 0.5.3, set by
    # its migration: only rows up to it can be marked as known. The race
    # was fixed in 0.5.2, so a fork after it is never offered. Null on an
    # install that started on 0.5.3 or later, which can't have race breaks.
    known_break_cutoff_row_id: int | None = None
    # From the last verify_chain() pass: breaks that match the race
    # signature but aren't recorded yet, and recorded ones it accepted.
    race_break_count: int = 0
    known_break_count: int = 0
    known_breaks_recorded_at: datetime | None = None


class AuditOutbox(SQLModel, table=True):
    """An audit event a migration had to record without the chain key (it
    has no access to it). The next start turns each one into an ordinary,
    signed AuditLog entry at the end of the chain and deletes it, in one
    transaction; only the actions app.audit_outbox accepts are recorded,
    any other is refused and audited as such (app/audit_outbox.py). Never
    a way into the existing chain: AuditLog rows are signed when written."""

    __tablename__ = "audit_outbox"

    id: int | None = Field(default=None, primary_key=True)
    action: str
    event_metadata: dict | None = Field(default=None, sa_column=Column("metadata", JSON))
    created_at: datetime = Field(default_factory=utcnow)
