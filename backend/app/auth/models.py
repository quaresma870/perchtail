from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Column
from sqlmodel import Field, Relationship, SQLModel

from app.timeutils import utcnow


class ScopeType(StrEnum):
    customer = "customer"
    source = "source"


class Capability(StrEnum):
    view = "view"
    download = "download"
    manage_rules = "manage_rules"
    run_now = "run_now"


class GlobalCapability(StrEnum):
    manage_users = "manage_users"
    manage_roles = "manage_roles"
    manage_sso = "manage_sso"
    create_source = "create_source"


class AuthProviderType(StrEnum):
    local = "local"
    oidc = "oidc"
    saml = "saml"


class SSOProtocol(StrEnum):
    oidc = "oidc"
    saml = "saml"


class Role(SQLModel, table=True):
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
    id: int | None = Field(default=None, primary_key=True)
    role_id: int = Field(foreign_key="role.id")
    # scope_id points at either customer.id or source.id depending on scope_type,
    # so it can't carry a single FK constraint — validated in auth/rbac.py (M2).
    scope_type: ScopeType
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
    role_id: int = Field(foreign_key="role.id")
    active: bool = True
    auth_provider: AuthProviderType = AuthProviderType.local
    external_id: str | None = None
    last_login_at: datetime | None = None
    # Set on admin-created local accounts (see CLAUDE.md's Security notes);
    # cleared once the user picks their own password.
    must_change_password: bool = False

    role: Role = Relationship(back_populates="users")


class SSOProviderConfig(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    protocol: SSOProtocol
    name: str
    # Encrypted at rest via app.crypto (client id/secret, or SAML metadata).
    config: str
    enabled: bool = False


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


class AuditLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="user.id")
    action: str
    target_type: str | None = None
    target_id: int | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    event_metadata: dict | None = Field(default=None, sa_column=Column("metadata", JSON))
