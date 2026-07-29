from enum import StrEnum
from typing import Optional

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
