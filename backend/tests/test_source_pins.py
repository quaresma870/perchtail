"""Deleting a source, or pointing it at another host, port or CA, removes
its old SSH host key and WinRM CA file, unless another source still uses
them (app/source_pins.py)."""

import hashlib
from types import SimpleNamespace

import paramiko
import pytest
from app.models import Protocol, Source
from app.source_pins import SourcePins, remove_unused_pins

CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


@pytest.fixture()
def places(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        data_dir=str(tmp_path), ssh_known_hosts_path=str(tmp_path / "ssh_known_hosts")
    )
    monkeypatch.setattr("app.source_pins.get_settings", lambda: settings)
    keys = paramiko.HostKeys()
    for name in ("app01", "[app02]:2222"):
        for key in (paramiko.RSAKey.generate(1024), paramiko.ECDSAKey.generate()):
            keys.add(name, key.get_name(), key)
    keys.save(settings.ssh_known_hosts_path)
    ca_dir = tmp_path / "winrm_ca"
    ca_dir.mkdir()
    ca_file = ca_dir / f"{hashlib.sha256(CA.encode()).hexdigest()}.pem"
    ca_file.write_text(CA)
    return SimpleNamespace(known_hosts=tmp_path / "ssh_known_hosts", ca_file=ca_file)


def _source(session, **values) -> Source:
    source = Source(name=values.pop("name", "s"), base_path="/", **values)
    session.add(source)
    session.commit()
    return source


def _pinned(path) -> set[str]:
    return set(paramiko.HostKeys(str(path)).keys())


def test_an_unused_host_key_goes_with_its_source(session, places):
    gone = SourcePins(Protocol.ssh, "app02", 2222, None)

    assert remove_unused_pins(session, gone) == ["host_key"]
    assert _pinned(places.known_hosts) == {"app01"}


def test_a_host_key_another_source_uses_stays(session, places):
    _source(session, protocol=Protocol.ssh, host="app01", port=None)
    gone = SourcePins(Protocol.ssh, "app01", 22, None)

    assert remove_unused_pins(session, gone) == []
    assert _pinned(places.known_hosts) == {"app01", "[app02]:2222"}


def test_an_unused_ca_file_goes_and_a_shared_one_stays(session, places):
    gone = SourcePins(Protocol.winrm, "win01", 5986, CA)
    other = _source(session, protocol=Protocol.winrm, host="win02", tls_ca_pem=CA)

    assert remove_unused_pins(session, gone) == []
    assert places.ca_file.exists()

    session.delete(other)
    session.commit()
    assert remove_unused_pins(session, gone) == ["ca_certificate"]
    assert not places.ca_file.exists()


def test_a_source_without_pins_removes_nothing(session, places):
    gone = SourcePins(Protocol.smb, "files01", None, None)

    assert remove_unused_pins(session, gone) == []
    assert _pinned(places.known_hosts) == {"app01", "[app02]:2222"}


# --- Through the API: deleting a source, or pointing it elsewhere ------------


@pytest.fixture()
def client(session):
    from app.api import errors as api_errors
    from app.api.auth import get_current_active_user
    from app.api.sources import router as sources_router
    from app.auth.models import Role, User
    from app.db import get_session
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    app = FastAPI()
    app.include_router(sources_router)
    api_errors.install(app)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _pins_removed(session) -> list[list[str]]:
    from app.auth.models import AuditLog
    from sqlmodel import select

    return [
        row.event_metadata["removed"]
        for row in session.exec(
            select(AuditLog).where(AuditLog.action == "source.pins_removed")
        ).all()
    ]


def test_deleting_a_source_removes_its_host_key_and_audits_it(session, places, client):
    source = _source(session, protocol=Protocol.ssh, host="app02", port=2222)

    assert client.delete(f"/sources/{source.id}").status_code == 204
    assert _pinned(places.known_hosts) == {"app01"}
    assert _pins_removed(session) == [["host_key"]]


@pytest.mark.parametrize("change", [{"host": "app03"}, {"port": 2200}])
def test_a_new_host_or_port_removes_the_old_host_key(session, places, client, change):
    source = _source(session, protocol=Protocol.ssh, host="app02", port=2222)

    assert client.patch(f"/sources/{source.id}", json=change).status_code == 200
    assert _pinned(places.known_hosts) == {"app01"}
    assert _pins_removed(session) == [["host_key"]]


def test_a_new_host_keeps_a_host_key_another_source_uses(session, places, client):
    source = _source(session, protocol=Protocol.ssh, host="app02", port=2222)
    _source(session, name="other", protocol=Protocol.ssh, host="app02", port=2222)

    assert client.patch(f"/sources/{source.id}", json={"host": "app03"}).status_code == 200
    assert _pinned(places.known_hosts) == {"app01", "[app02]:2222"}
    assert _pins_removed(session) == []


def test_a_removed_ca_file_goes_with_the_change(session, places, client):
    source = _source(session, protocol=Protocol.winrm, host="win01", tls_ca_pem=CA)

    response = client.patch(f"/sources/{source.id}", json={"tls_ca_pem": ""})
    assert response.status_code == 200
    assert not places.ca_file.exists()
    assert _pins_removed(session) == [["ca_certificate"]]


def test_other_changes_keep_the_pins(session, places, client):
    source = _source(session, protocol=Protocol.ssh, host="app02", port=2222)
    winrm = _source(session, name="w", protocol=Protocol.winrm, host="win01", tls_ca_pem=CA)

    assert client.patch(f"/sources/{source.id}", json={"name": "renamed"}).status_code == 200
    assert client.patch(f"/sources/{winrm.id}", json={"name": "w2"}).status_code == 200
    assert _pinned(places.known_hosts) == {"app01", "[app02]:2222"}
    assert places.ca_file.exists()
    assert _pins_removed(session) == []
