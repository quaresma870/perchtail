"""WinRM: explicit scheme and a per-source CA certificate (#169)."""

import datetime

import app.collectors.winrm as winrm_module
import pytest
from app.api import errors as api_errors
from app.api.auth import get_current_active_user
from app.api.sources import router as sources_router
from app.auth.models import AuditLog, Role, User
from app.config import get_settings
from app.db import get_session
from app.models import Protocol, Source
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


def _ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lab Root CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.mark.parametrize(
    "scheme, port, expected",
    [
        (None, None, ("https", 5986)),
        (None, 5986, ("https", 5986)),
        (None, 5985, ("http", 5985)),
        (None, 8443, ("http", 8443)),  # the pre-0.5.0 rule, kept for unset sources
        ("https", None, ("https", 5986)),
        ("https", 8443, ("https", 8443)),
        ("http", None, ("http", 5985)),
    ],
)
def test_endpoint(scheme, port, expected):
    source = Source(name="w", protocol=Protocol.winrm, host="h", base_path="C:\\", port=port)
    source.winrm_scheme = scheme
    assert winrm_module.endpoint(source) == expected


def test_session_trusts_the_source_ca(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    pem = _ca_pem()
    source = Source(name="w", protocol=Protocol.winrm, host="win01", base_path="D:\\", port=8443)
    source.winrm_scheme = "https"
    source.tls_ca_pem = pem

    target, kwargs = winrm_module.session_target(source)

    assert target == "https://win01:8443/wsman"
    assert kwargs["server_cert_validation"] == "validate"
    assert kwargs["transport"] == "ntlm"
    ca_path = kwargs["ca_trust_path"]
    assert ca_path.startswith(str(tmp_path / "winrm_ca"))
    assert open(ca_path).read() == pem

    source.tls_ca_pem = None
    assert "ca_trust_path" not in winrm_module.session_target(source)[1]
    source.winrm_scheme, source.tls_ca_pem = "http", pem
    assert "ca_trust_path" not in winrm_module.session_target(source)[1]


@pytest.fixture()
def client(session):
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


def _create(client, **overrides):
    body = {
        "name": "win01",
        "protocol": "winrm",
        "host": "win01",
        "base_path": "D:\\Logs",
        "credential": {"username": "u", "password": "p"},
    }
    body.update(overrides)
    return client.post("/sources", json=body)


def test_new_winrm_sources_store_an_explicit_scheme(client):
    assert _create(client).json()["winrm_scheme"] == "https"
    assert _create(client, port=5985).json()["winrm_scheme"] == "http"
    body = _create(client, port=8443, winrm_scheme="https").json()
    assert (body["winrm_scheme"], body["winrm_effective_scheme"]) == ("https", "https")


def test_ca_certificate_is_validated_and_audited(session, client):
    bad = _create(client, tls_ca_pem="-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----")
    assert bad.status_code == 400
    assert "isn't valid PEM" in bad.json()["detail"]

    created = _create(client, tls_ca_pem=_ca_pem()).json()
    assert created["tls_ca_pem"].startswith("-----BEGIN CERTIFICATE-----")

    patched = client.patch(f"/sources/{created['id']}", json={"tls_ca_pem": ""}).json()
    assert patched["tls_ca_pem"] is None
    event = session.exec(select(AuditLog).where(AuditLog.action == "source.update")).one()
    assert event.event_metadata["changes"]["tls_ca_pem"] == "removed"


def test_scheme_and_ca_are_winrm_only(client):
    response = client.post(
        "/sources",
        json={
            "name": "s",
            "protocol": "ssh",
            "host": "h",
            "base_path": "/",
            "winrm_scheme": "https",
        },
    )
    assert response.status_code == 400
    response = client.post(
        "/sources",
        json={
            "name": "s",
            "protocol": "ssh",
            "host": "h",
            "base_path": "/",
            "tls_ca_pem": _ca_pem(),
        },
    )
    assert response.status_code == 400


def test_existing_sources_keep_inferring_until_edited(session, client):
    source = Source(name="old", protocol=Protocol.winrm, host="h", base_path="D:\\", port=5985)
    session.add(source)
    session.commit()
    body = client.get(f"/sources/{source.id}").json()
    assert body["winrm_scheme"] is None
    assert body["winrm_effective_scheme"] == "http"


# --- JEA endpoint (#169) ------------------------------------------------------


def test_a_winrm_source_can_name_a_jea_endpoint(session, client):
    created = _create(client, winrm_configuration=" PerchTail ").json()
    assert created["winrm_configuration"] == "PerchTail"
    assert _create(client).json()["winrm_configuration"] is None

    for bad in ("Perch Tail", "x;y", "a/b", "'quoted'"):
        response = _create(client, winrm_configuration=bad)
        assert response.status_code == 400, bad

    # Back to the WinRS shell with an empty name; the change is audited.
    patched = client.patch(f"/sources/{created['id']}", json={"winrm_configuration": ""}).json()
    assert patched["winrm_configuration"] is None
    event = session.exec(select(AuditLog).where(AuditLog.action == "source.update")).one()
    assert event.event_metadata["changes"]["winrm_configuration"] == {
        "from": "PerchTail",
        "to": None,
    }


def test_a_jea_endpoint_is_winrm_only(client):
    response = client.post(
        "/sources",
        json={
            "name": "s",
            "protocol": "ssh",
            "host": "h",
            "base_path": "/",
            "winrm_configuration": "PerchTail",
        },
    )
    assert response.status_code == 400
