import pytest
from app.config import get_settings
from app.crypto import _fernet, decrypt_secret, encrypt_secret
from cryptography.fernet import InvalidToken


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-key-for-unit-tests")
    get_settings.cache_clear()
    _fernet.cache_clear()

    ciphertext = encrypt_secret("super-secret-ssh-key")
    assert ciphertext != "super-secret-ssh-key"
    assert decrypt_secret(ciphertext) == "super-secret-ssh-key"

    get_settings.cache_clear()
    _fernet.cache_clear()


def test_different_keys_cannot_decrypt_each_others_ciphertext(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "key-one")
    get_settings.cache_clear()
    _fernet.cache_clear()
    ciphertext = encrypt_secret("a-password")

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "key-two")
    get_settings.cache_clear()
    _fernet.cache_clear()

    with pytest.raises(InvalidToken):
        decrypt_secret(ciphertext)

    get_settings.cache_clear()
    _fernet.cache_clear()
