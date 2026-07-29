from enum import StrEnum

from sqlmodel import Field, Relationship, SQLModel


class Protocol(StrEnum):
    ssh = "ssh"
    smb = "smb"
    winrm = "winrm"
    local = "local"


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


class Source(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
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

    customer: Customer | None = Relationship(back_populates="sources")
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
