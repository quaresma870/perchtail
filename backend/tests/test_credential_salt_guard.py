import pytest
from app.auth.models import AuditLog, SSOProtocol, SSOProviderConfig, User
from app.bootstrap import CredentialSaltMissingError, assert_credential_salt_present
from app.config import get_settings
from app.models import Protocol, Source


@pytest.fixture()
def salt_path(tmp_path, monkeypatch):
    path = tmp_path / "credential_salt"
    monkeypatch.setattr(get_settings(), "credential_salt_path", str(path))
    monkeypatch.setattr(get_settings(), "credential_salt_allow_regenerate", False)
    return path


def _add_encrypted_source(session):
    session.add(
        Source(name="s", protocol=Protocol.ssh, host="h", base_path="/", credential_ref="blob")
    )
    session.commit()


def test_fresh_database_without_salt_starts_normally(session, salt_path):
    assert_credential_salt_present(session)


def test_existing_salt_starts_normally(session, salt_path):
    salt_path.write_bytes(b"0" * 16)
    _add_encrypted_source(session)
    assert_credential_salt_present(session)


@pytest.mark.parametrize(
    "make_row",
    [
        lambda: Source(
            name="s", protocol=Protocol.ssh, host="h", base_path="/", credential_ref="blob"
        ),
        lambda: SSOProviderConfig(protocol=SSOProtocol.oidc, name="idp", config="blob"),
        lambda: AuditLog(action="user.login", row_hash="abc"),
    ],
)
def test_missing_salt_with_encrypted_data_refuses_to_start(session, salt_path, make_row):
    session.add(make_row())
    session.commit()
    with pytest.raises(CredentialSaltMissingError):
        assert_credential_salt_present(session)
    assert not salt_path.exists()


def test_missing_salt_with_mfa_secret_refuses_to_start(session, salt_path):
    from app.auth.models import Role

    role = Role(name="r")
    session.add(role)
    session.commit()
    session.add(User(username="u", role_id=role.id, mfa_secret_encrypted="blob"))
    session.commit()
    with pytest.raises(CredentialSaltMissingError):
        assert_credential_salt_present(session)


def test_escape_hatch_allows_regenerating(session, salt_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "credential_salt_allow_regenerate", True)
    _add_encrypted_source(session)
    assert_credential_salt_present(session)


def test_dockerfile_keeps_salt_and_known_hosts_on_the_data_volume():
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    assert "CREDENTIAL_SALT_PATH=/data/credential_salt" in dockerfile
    assert "SSH_KNOWN_HOSTS_PATH=/data/ssh_known_hosts" in dockerfile
