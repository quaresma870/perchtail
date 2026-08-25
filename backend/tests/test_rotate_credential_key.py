import json

import pytest
from app.auth.models import SSOProtocol, SSOProviderConfig
from app.config import get_settings
from app.crypto import build_fernet
from app.models import Protocol, Source
from app.rotate_credential_key import rotate
from cryptography.fernet import InvalidToken
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


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
