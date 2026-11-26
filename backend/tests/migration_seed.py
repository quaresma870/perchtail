"""A populated PerchTail database for the SQLite to PostgreSQL migration
tests (tests/test_db_copy.py, tests/test_migrate_database.py): one row or
more of every kind of object, every kind of stored secret, the file-name
index, and an audit chain with a purge and its anchor. Also imported by the
scripts those tests run in a subprocess."""

import json

from app.audit import record_audit_event
from app.audit_purge import _purge_chunk
from app.auth.models import (
    AuditLog,
    Capability,
    Role,
    RoleGrant,
    ScopeType,
    SSOProtocol,
    SSOProviderConfig,
    User,
)
from app.auth.providers.local import hash_password
from app.crypto import encrypt_credential, encrypt_secret
from app.models import (
    Alert,
    Customer,
    FileNameEntry,
    Folder,
    Protocol,
    Rule,
    RuleType,
    SmtpSettings,
    Source,
)
from app.timeutils import utcnow
from sqlalchemy import func
from sqlmodel import Session, select

READER = "migrated-reader"
READER_PASSWORD = "Migr4ted-Reader-Passw0rd"
CREDENTIAL = {"username": "svc", "password": "ssh-s3cret"}
INDEXED_FILES = 2500
BIG = 2**40


def populate(session: Session, engine) -> dict:
    """Fills the database `session` and `engine` are on, commits, and
    returns what the tests compare after the migration."""
    customer = Customer(name="Migrated customer")
    session.add(customer)
    session.flush()
    # A folder inside one with a higher id: the copy can't write them in id
    # order with the parent column filled.
    child = Folder(name="child", customer_id=customer.id)
    session.add(child)
    session.flush()
    parent = Folder(name="parent", customer_id=customer.id)
    session.add(parent)
    session.flush()
    child.parent_folder_id = parent.id
    session.add(child)
    source = Source(
        name="migrated-app01",
        protocol=Protocol.ssh,
        host="app01.example.test",
        base_path="/var/log",
        customer_id=customer.id,
        folder_id=child.id,
        credential_ref=encrypt_credential(CREDENTIAL),
    )
    session.add(source)
    session.flush()
    session.add(Rule(source_id=source.id, order=0, type=RuleType.include, pattern="**"))
    now = utcnow()
    for number in range(INDEXED_FILES):
        session.add(
            FileNameEntry(
                source_id=source.id,
                path=f"logs/app-{number:04d}.log",
                size=BIG + number,
                mtime=1_700_000_000.5 + number,
                seen_at=now,
            )
        )
    role = Role(name="migrated readers")
    session.add(role)
    session.flush()
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.customer,
            scope_id=customer.id,
            capabilities=[Capability.view],
        )
    )
    reader = User(
        username=READER,
        role_id=role.id,
        password_hash=hash_password(READER_PASSWORD),
        password_changed_at=now,
    )
    mfa_user = User(
        username="migrated-mfa",
        role_id=role.id,
        mfa_enabled=True,
        mfa_secret_encrypted=encrypt_secret("JBSWY3DPEHPK3PXP"),
    )
    session.add(reader)
    session.add(mfa_user)
    session.flush()
    session.add(
        SSOProviderConfig(
            protocol=SSOProtocol.oidc,
            name="migrated-idp",
            config=encrypt_secret(json.dumps({"client_secret": "oidc-s3cret"})),
            enabled=False,
        )
    )
    session.add(
        SmtpSettings(
            host="smtp.example.test",
            port=587,
            username="relay",
            password_encrypted=encrypt_secret("smtp-s3cret"),
            from_address="perchtail@example.test",
            updated_at=now,
        )
    )
    session.add(
        Alert(
            user_id=reader.id,
            name="errors",
            query="ERROR",
            webhook_url_encrypted=encrypt_secret("https://hooks.example.test/s3cret"),
            created_at=now,
        )
    )
    for number in range(6):
        record_audit_event(
            session,
            user_id=reader.id,
            action="customer.update",
            target_type="customer",
            target_id=customer.id,
            metadata={"step": number, "nothing": None},
        )
    session.commit()
    # A purge of the oldest audit rows leaves an anchor the copy keeps.
    first = session.exec(select(func.min(AuditLog.id))).one()
    _purge_chunk(engine, first + 2, 365)
    return {"customer_id": customer.id, "child_id": child.id, "parent_id": parent.id}
