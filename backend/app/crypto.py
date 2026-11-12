import base64
import json
import os
import secrets
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config import get_settings

# OWASP's current floor for PBKDF2-HMAC-SHA256 (2023 cheat sheet). A single
# unsalted SHA-256 round -- the previous implementation -- has no work
# factor at all, so even a reasonably long CREDENTIAL_ENCRYPTION_KEY was far
# cheaper to brute-force than it should be.
_PBKDF2_ITERATIONS = 600_000
_KEY_LENGTH_BYTES = 32


def _load_or_create_salt(path: Path) -> bytes:
    """A per-install random salt, generated once and persisted next to the
    database rather than derived from anything -- reusing the same salt
    across every install would defeat its purpose (cross-deployment
    rainbow-table resistance), and it has to stay stable across restarts or
    every previously-encrypted credential becomes undecryptable the moment
    it changes."""
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    # Readable by this user only (#193); O_EXCL so two workers starting at
    # once can't each write a different salt.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(fd, "wb") as fh:
        fh.write(salt)
    return salt


def _derive_fernet_key(secret: str) -> bytes:
    salt = _load_or_create_salt(Path(get_settings().credential_salt_path))
    return fernet_key_for_salt(secret, salt)


def fernet_key_for_salt(secret: str, salt: bytes) -> bytes:
    """The Fernet key for a CREDENTIAL_ENCRYPTION_KEY and a given salt, for
    app/backup.py, which checks a backup's credentials against the salt in
    the backup rather than the one on disk."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH_BYTES,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))


def _derive_audit_chain_key(secret: str) -> bytes:
    """A second key derived from the same root secret
    (CREDENTIAL_ENCRYPTION_KEY) and the same per-install salt as the Fernet
    key above, but domain-separated from it by folding a fixed,
    purpose-specific label into the KDF's input material rather than by
    using a different salt. This is deliberately NOT a refactor of
    _derive_fernet_key -- that derivation must never change, or every
    already-encrypted credential in an existing deployment becomes
    undecryptable (see its own "breaking for existing deployments"
    history). Used to key app/audit_hash_chain.py's HMAC so the audit-log
    hash chain can't be recomputed by anyone who only has the SQLite file
    (a stolen backup, or limited SQL access) -- a bare, keyless hash chain
    is fully reproducible from public data with the same public algorithm
    this project ships, which would make tampering silently undetectable
    against exactly the "compromised admin account" threat this feature
    exists for."""
    salt = _load_or_create_salt(Path(get_settings().credential_salt_path))
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH_BYTES,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(f"perchtail-audit-chain-hmac:{secret}".encode())


@lru_cache
def audit_chain_key() -> bytes:
    return _derive_audit_chain_key(get_settings().credential_encryption_key)


def build_audit_chain_key(secret: str) -> bytes:
    """The audit chain key for a given CREDENTIAL_ENCRYPTION_KEY, for
    app/rotate_credential_key.py, which needs the old and the new one."""
    return _derive_audit_chain_key(secret)


def build_fernet(secret: str) -> Fernet:
    """Exposed (not just the cached singleton below) for
    app/rotate_credential_key.py, which needs two independent Fernet
    instances at once -- one for the old CREDENTIAL_ENCRYPTION_KEY, one for
    the new -- neither of which is necessarily what get_settings() reports
    at the time it runs."""
    return Fernet(_derive_fernet_key(secret))


@lru_cache
def _fernet() -> Fernet:
    return build_fernet(get_settings().credential_encryption_key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str, *, ttl: int | None = None) -> str:
    """`ttl` (seconds) rejects a token older than that, using the creation
    timestamp Fernet already embeds in every token — used for the OIDC
    login flow's short-lived state param rather than a server-side state
    table (`app/auth/providers/oidc.py`)."""
    return _fernet().decrypt(ciphertext.encode("utf-8"), ttl=ttl).decode("utf-8")


def encrypt_credential(data: dict) -> str:
    """Source.credential_ref holds an encrypted JSON blob rather than a bare
    secret, since each protocol needs different fields (SSH: username +
    private_key or password; SMB/WinRM: username + password)."""
    return encrypt_secret(json.dumps(data))


def decrypt_credential(ciphertext: str) -> dict:
    return json.loads(decrypt_secret(ciphertext))
