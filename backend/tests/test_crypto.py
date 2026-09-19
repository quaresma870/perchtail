import pytest
from app.config import get_settings
from app.crypto import (
    _derive_audit_chain_key,
    _derive_fernet_key,
    _fernet,
    audit_chain_key,
    decrypt_credential,
    decrypt_secret,
    encrypt_credential,
    encrypt_secret,
)
from cryptography.fernet import InvalidToken


def _reset_caches():
    get_settings.cache_clear()
    _fernet.cache_clear()
    audit_chain_key.cache_clear()


@pytest.fixture(autouse=True)
def _clear_caches_around_each_test():
    _reset_caches()
    yield
    _reset_caches()


def _configure(monkeypatch, tmp_path, *, key: str, salt_subdir: str = "salt"):
    """Every test gets its own tmp_path-scoped salt file -- without this,
    _derive_fernet_key's default (./data/credential_salt, relative to
    wherever pytest runs from) would read/write a real file in the repo's
    own data/ directory as a side effect of running the test suite."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    monkeypatch.setenv("CREDENTIAL_SALT_PATH", str(tmp_path / salt_subdir / "credential_salt"))
    _reset_caches()


def test_encrypt_decrypt_roundtrip(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, key="test-key-for-unit-tests")

    ciphertext = encrypt_secret("super-secret-ssh-key")
    assert ciphertext != "super-secret-ssh-key"
    assert decrypt_secret(ciphertext) == "super-secret-ssh-key"


def test_different_keys_cannot_decrypt_each_others_ciphertext(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, key="key-one")
    ciphertext = encrypt_secret("a-password")

    _configure(monkeypatch, tmp_path, key="key-two")

    with pytest.raises(InvalidToken):
        decrypt_secret(ciphertext)


def test_encrypt_decrypt_credential_roundtrip(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, key="test-key-for-unit-tests")

    ciphertext = encrypt_credential({"username": "perchtail", "password": "s3cret!"})
    assert "s3cret!" not in ciphertext
    assert decrypt_credential(ciphertext) == {"username": "perchtail", "password": "s3cret!"}


# --- key derivation / salt persistence ----------------------------------------------


def test_derive_fernet_key_creates_a_salt_file(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, key="a-key")
    salt_path = tmp_path / "salt" / "credential_salt"
    assert not salt_path.exists()

    _derive_fernet_key("a-key")

    assert salt_path.exists()
    assert len(salt_path.read_bytes()) == 16


def test_derive_fernet_key_is_stable_across_calls(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, key="a-key")

    first = _derive_fernet_key("a-key")
    second = _derive_fernet_key("a-key")

    assert first == second


def test_derive_fernet_key_reuses_the_persisted_salt_not_a_fresh_one(monkeypatch, tmp_path):
    """The real-world scenario this guards: an app restart must derive the
    *same* key from the same CREDENTIAL_ENCRYPTION_KEY, or every previously
    encrypted credential becomes permanently undecryptable."""
    _configure(monkeypatch, tmp_path, key="a-key")
    first = _derive_fernet_key("a-key")

    # Simulates a fresh process: caches cleared, salt file already on disk
    # from the call above.
    _reset_caches()
    second = _derive_fernet_key("a-key")

    assert first == second


def test_derive_fernet_key_differs_across_installs_with_different_salts(monkeypatch, tmp_path):
    """Same passphrase, two different "installs" (different salt files) --
    proves the salt actually participates in derivation rather than being
    generated-and-ignored."""
    _configure(monkeypatch, tmp_path, key="same-passphrase", salt_subdir="install-a")
    key_a = _derive_fernet_key("same-passphrase")

    _configure(monkeypatch, tmp_path, key="same-passphrase", salt_subdir="install-b")
    key_b = _derive_fernet_key("same-passphrase")

    assert key_a != key_b


def test_derive_fernet_key_is_not_a_bare_sha256_digest(monkeypatch, tmp_path):
    """Regression guard for the original vulnerability: a single unsalted
    SHA-256 round is fully reproducible from the passphrase alone, with no
    persisted state. Confirms derivation now depends on something beyond
    just hashlib.sha256(secret)."""
    import base64
    import hashlib

    _configure(monkeypatch, tmp_path, key="a-key")
    derived = _derive_fernet_key("a-key")

    naive_sha256 = base64.urlsafe_b64encode(hashlib.sha256(b"a-key").digest())
    assert derived != naive_sha256


# --- audit chain HMAC key (app/audit_hash_chain.py) ---------------------------


def test_audit_chain_key_differs_from_the_fernet_key(monkeypatch, tmp_path):
    # Same root secret, same salt file, but domain-separated by a different
    # KDF input label -- see _derive_audit_chain_key's own docstring for
    # why these must never be the same value (or trivially derivable from
    # each other).
    _configure(monkeypatch, tmp_path, key="a-key")

    fernet_key = _derive_fernet_key("a-key")
    chain_key = _derive_audit_chain_key("a-key")

    assert chain_key != fernet_key


def test_audit_chain_key_is_stable_across_calls(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, key="a-key")

    first = _derive_audit_chain_key("a-key")
    second = _derive_audit_chain_key("a-key")

    assert first == second


def test_audit_chain_key_reuses_the_persisted_salt_not_a_fresh_one(monkeypatch, tmp_path):
    """Same real-world scenario as _derive_fernet_key's equivalent test: a
    restart must derive the *same* key, or every AuditLog row written
    before it becomes permanently unverifiable."""
    _configure(monkeypatch, tmp_path, key="a-key")
    first = _derive_audit_chain_key("a-key")

    _reset_caches()
    second = _derive_audit_chain_key("a-key")

    assert first == second


def test_audit_chain_key_differs_across_installs_with_different_salts(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, key="same-passphrase", salt_subdir="install-a")
    key_a = _derive_audit_chain_key("same-passphrase")

    _configure(monkeypatch, tmp_path, key="same-passphrase", salt_subdir="install-b")
    key_b = _derive_audit_chain_key("same-passphrase")

    assert key_a != key_b


def test_audit_chain_key_singleton_reads_from_settings(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, key="a-key")

    assert audit_chain_key() == _derive_audit_chain_key("a-key")
