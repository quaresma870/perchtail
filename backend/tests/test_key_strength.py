"""CREDENTIAL_ENCRYPTION_KEY strength (app/key_strength.py): the documented
generators pass, short or repetitive keys are reported, and a weak key stops
the start unless CREDENTIAL_KEY_REFUSE_WEAK is false, which only warns (at
startup and on the system settings pages)."""

import base64
import secrets

import pytest
from app.key_strength import decoded_key_bytes, entropy_bits, weaknesses


@pytest.mark.parametrize(
    "key",
    [
        secrets.token_urlsafe(32),
        base64.b64encode(secrets.token_bytes(32)).decode(),
        secrets.token_hex(32),
        "correct horse battery staple and a few more words",
    ],
)
def test_strong_keys_pass(key):
    assert weaknesses(key) == []


@pytest.mark.parametrize(
    "key",
    [
        "hunter2",
        secrets.token_hex(16),
        "a" * 64,
        "password" * 4,
        "changeme" * 6,
        "password" * 8,
    ],
)
def test_weak_keys_are_reported(key):
    assert weaknesses(key)


def test_a_repeated_phrase_counts_about_one_period():
    # "changeme" * 6 decodes to 36 bytes and its characters alone give about
    # 132 bits; compressed it's little more than the word itself.
    assert len(decoded_key_bytes("changeme" * 6)) >= 32
    assert entropy_bits("changeme" * 6) < 128
    assert entropy_bits("password" * 8) < 128


def test_encoded_keys_count_their_decoded_bytes():
    assert len(decoded_key_bytes(secrets.token_hex(32))) == 32
    assert len(decoded_key_bytes(secrets.token_urlsafe(32))) == 32
    assert decoded_key_bytes("not base64 at all!") == b"not base64 at all!"


def test_entropy_of_a_repeated_character_is_zero():
    assert entropy_bits("x" * 100) == 0
    assert entropy_bits("") == 0


def test_the_warnings_endpoint_reports_a_weak_key(session, monkeypatch):
    from app.api.auth import get_current_active_user
    from app.api.system_settings import router
    from app.auth.models import Role, User
    from app.config import Settings
    from app.db import get_session
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    role = Role(name="warnings admin", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="warnings-admin", role_id=role.id)
    session.add(user)
    session.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    client = TestClient(app)

    monkeypatch.setattr(
        "app.api.system_settings.get_settings",
        lambda: Settings(credential_encryption_key="short-key"),
    )
    warnings = client.get("/system-settings/warnings").json()
    assert [w["code"] for w in warnings] == ["weak_credential_encryption_key"]
    assert "short-key" not in str(warnings)
    # Neither the length nor the estimated bits reach a delegated admin.
    assert not any(char.isdigit() for char in warnings[0]["message"])
    assert "bytes" not in str(warnings) and "bits" not in str(warnings)

    monkeypatch.setattr(
        "app.api.system_settings.get_settings",
        lambda: Settings(credential_encryption_key=secrets.token_urlsafe(32)),
    )
    assert client.get("/system-settings/warnings").json() == []
