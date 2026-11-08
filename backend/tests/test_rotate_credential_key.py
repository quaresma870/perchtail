import json

import pytest
from app.auth.models import SSOProtocol, SSOProviderConfig
from app.config import get_settings
from app.crypto import build_fernet
from app.models import Protocol, Source
from app.rotate_credential_key import rotate
from cryptography.fernet import InvalidToken
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


def _configure_salt(monkeypatch, tmp_path):
    # Same isolation reasoning as test_crypto.py's _configure: without this,
    # the salt would be read/written from the repo's own ./data directory.
    monkeypatch.setenv("CREDENTIAL_SALT_PATH", str(tmp_path / "credential_salt"))
    get_settings.cache_clear()


def _throwaway_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture()
def rotation_engine(monkeypatch, tmp_path):
    _configure_salt(monkeypatch, tmp_path)
    engine = _throwaway_engine()
    monkeypatch.setattr("app.db.engine", engine)
    yield engine
    get_settings.cache_clear()


def _make_source(session, old_fernet, *, name="app01", credential=None) -> Source:
    credential_ref = None
    if credential is not None:
        credential_ref = old_fernet.encrypt(json.dumps(credential).encode("utf-8")).decode("utf-8")
    source = Source(
        name=name,
        protocol=Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log",
        credential_ref=credential_ref,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _make_sso_config(session, old_fernet, *, name="Okta", config=None) -> SSOProviderConfig:
    config = config or {"client_id": "abc", "client_secret": "shh"}
    row = SSOProviderConfig(
        protocol=SSOProtocol.oidc,
        name=name,
        config=old_fernet.encrypt(json.dumps(config).encode("utf-8")).decode("utf-8"),
        enabled=False,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_rotate_re_encrypts_source_credentials_under_the_new_key(rotation_engine):
    old_fernet = build_fernet("old-key")
    with Session(rotation_engine) as session:
        source = _make_source(
            session, old_fernet, credential={"username": "root", "password": "hunter2"}
        )
        source_id = source.id

    result = rotate(old_key="old-key", new_key="new-key", dry_run=False)
    assert result == 0

    new_fernet = build_fernet("new-key")
    with Session(rotation_engine) as session:
        refreshed = session.get(Source, source_id)
        # Old key must no longer work...
        with pytest.raises(InvalidToken):
            old_fernet.decrypt(refreshed.credential_ref.encode("utf-8"))
        # ...and the new key must decrypt back to the original plaintext.
        plaintext = new_fernet.decrypt(refreshed.credential_ref.encode("utf-8"))
        assert plaintext == b'{"username": "root", "password": "hunter2"}'


def test_rotate_re_encrypts_sso_provider_configs(rotation_engine):
    old_fernet = build_fernet("old-key")
    with Session(rotation_engine) as session:
        row = _make_sso_config(session, old_fernet)
        row_id = row.id

    rotate(old_key="old-key", new_key="new-key", dry_run=False)

    new_fernet = build_fernet("new-key")
    with Session(rotation_engine) as session:
        refreshed = session.get(SSOProviderConfig, row_id)
        assert new_fernet.decrypt(refreshed.config.encode("utf-8"))


def test_rotate_skips_sources_with_no_credential(rotation_engine):
    old_fernet = build_fernet("old-key")
    with Session(rotation_engine) as session:
        _make_source(session, old_fernet, credential=None)  # e.g. protocol=local

    result = rotate(old_key="old-key", new_key="new-key", dry_run=False)
    assert result == 0


def test_dry_run_reports_success_without_writing_anything(rotation_engine):
    old_fernet = build_fernet("old-key")
    with Session(rotation_engine) as session:
        source = _make_source(session, old_fernet, credential={"username": "root", "password": "x"})
        original_ciphertext = source.credential_ref
        source_id = source.id

    result = rotate(old_key="old-key", new_key="new-key", dry_run=True)
    assert result == 0

    with Session(rotation_engine) as session:
        refreshed = session.get(Source, source_id)
        assert refreshed.credential_ref == original_ciphertext


def test_rotate_aborts_and_writes_nothing_on_wrong_old_key(rotation_engine):
    old_fernet = build_fernet("old-key")
    with Session(rotation_engine) as session:
        source = _make_source(
            session, old_fernet, credential={"username": "root", "password": "hunter2"}
        )
        original_ciphertext = source.credential_ref
        source_id = source.id
        _make_sso_config(session, old_fernet)

    result = rotate(old_key="totally-wrong-key", new_key="new-key", dry_run=False)
    assert result == 1

    with Session(rotation_engine) as session:
        refreshed = session.get(Source, source_id)
        assert refreshed.credential_ref == original_ciphertext


# --- MFA secrets and the audit chain (#326) -----------------------------------


def _audit(session, key, action):
    from app.audit_hash_chain import chain_new_entry
    from app.auth.models import AuditLog
    from app.timeutils import utcnow

    entry = AuditLog(user_id=None, action=action, timestamp=utcnow(), event_metadata={"n": 1})
    chain_new_entry(key, session, entry)
    session.add(entry)
    session.commit()
    return entry.id


def _mfa_user(session, old_fernet):
    from app.auth.models import Role, User

    role = Role(name="r", is_super_admin=False)
    session.add(role)
    session.commit()
    user = User(
        username="mfa-user",
        role_id=role.id,
        mfa_enabled=True,
        mfa_secret_encrypted=old_fernet.encrypt(b"JBSWY3DPEHPK3PXP").decode("utf-8"),
    )
    session.add(user)
    session.commit()
    return user.id


def test_rotate_re_encrypts_mfa_secrets_and_re_signs_the_audit_chain(rotation_engine):
    from app.audit_integrity import verify_chain
    from app.auth.models import AuditLog, User
    from app.crypto import build_audit_chain_key
    from sqlmodel import select

    old_fernet = build_fernet("old-key")
    old_chain = build_audit_chain_key("old-key")
    with Session(rotation_engine) as session:
        user_id = _mfa_user(session, old_fernet)
        for n in range(3):
            _audit(session, old_chain, f"test.event{n}")

    assert rotate(old_key="old-key", new_key="new-key", dry_run=False) == 0

    with Session(rotation_engine) as session:
        secret = session.get(User, user_id).mfa_secret_encrypted
        assert build_fernet("new-key").decrypt(secret.encode("utf-8")) == b"JBSWY3DPEHPK3PXP"
        assert verify_chain(build_audit_chain_key("new-key"), session).ok
        assert not verify_chain(old_chain, session).ok
        last = session.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
        assert last.action == "credential_key.rotate"
        assert last.event_metadata["mfa_secrets"] == 1
        assert last.event_metadata["audit_rows_resigned"] == 3


def test_a_broken_audit_chain_stops_the_rotation_with_nothing_written(rotation_engine):
    from app.auth.models import AuditLog
    from app.crypto import build_audit_chain_key

    old_fernet = build_fernet("old-key")
    old_chain = build_audit_chain_key("old-key")
    with Session(rotation_engine) as session:
        source = _make_source(session, old_fernet, credential={"username": "u", "password": "p"})
        source_id, before = source.id, source.credential_ref
        _audit(session, old_chain, "test.one")
        tampered_id = _audit(session, old_chain, "test.two")
        _audit(session, old_chain, "test.three")
        row = session.get(AuditLog, tampered_id)
        row.action = "test.edited"  # tampering: the row no longer matches its hash
        session.add(row)
        session.commit()

    assert rotate(old_key="old-key", new_key="new-key", dry_run=False) == 1

    with Session(rotation_engine) as session:
        assert session.get(Source, source_id).credential_ref == before


def test_dry_run_checks_the_chain_without_writing(rotation_engine):
    from app.auth.models import AuditLog
    from app.crypto import build_audit_chain_key
    from sqlmodel import select

    old_chain = build_audit_chain_key("old-key")
    with Session(rotation_engine) as session:
        _audit(session, old_chain, "test.one")
        before = session.exec(select(AuditLog.row_hash)).all()

    assert rotate(old_key="old-key", new_key="new-key", dry_run=True) == 0

    with Session(rotation_engine) as session:
        assert session.exec(select(AuditLog.row_hash)).all() == before


def test_a_chain_with_recorded_known_breaks_can_still_be_rotated(rotation_engine, monkeypatch):
    from app.audit_hash_chain import compute_row_hash
    from app.audit_integrity import record_known_breaks, verify_chain
    from app.auth.models import AuditChainState, AuditLog
    from app.crypto import build_audit_chain_key
    from app.timeutils import utcnow

    old_chain = build_audit_chain_key("old-key")
    # The known-break entry is chained with the configured key, as it would
    # be; app.audit calls the cached key function, so it's replaced there.
    monkeypatch.setattr("app.audit.audit_chain_key", lambda: old_chain)
    with Session(rotation_engine) as session:
        _audit(session, old_chain, "before")
        tip_id = _audit(session, old_chain, "tip")
        _audit(session, old_chain, "first")
        tip = session.get(AuditLog, tip_id)
        forked = []
        for action in ("second", "third"):
            # The #213 race: valid rows chained from the tip, not the newest row.
            entry = AuditLog(user_id=None, action=action, timestamp=utcnow())
            entry.prev_hash = tip.row_hash
            entry.row_hash = compute_row_hash(
                old_chain,
                tip.row_hash,
                user_id=None,
                action=action,
                target_type=None,
                target_id=None,
                timestamp=entry.timestamp,
                metadata=None,
            )
            session.add(entry)
            session.commit()
            forked.append(entry.id)
        _audit(session, old_chain, "after")
        state = session.exec(select(AuditChainState)).first() or AuditChainState()
        state.known_break_cutoff_row_id = forked[-1]
        session.add(state)
        session.commit()
        recorded = record_known_breaks(old_chain, session, user_id=None)
        assert recorded.ok, recorded
        session.commit()

    assert rotate(old_key="old-key", new_key="new-key", dry_run=False) == 0
    with Session(rotation_engine) as session:
        assert verify_chain(build_audit_chain_key("new-key"), session).ok
