import base64
import json
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
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    path.write_bytes(salt)
    return salt


def _derive_fernet_key(secret: str) -> bytes:
    salt = _load_or_create_salt(Path(get_settings().credential_salt_path))
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH_BYTES,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))


@lru_cache
def _fernet() -> Fernet:
    return Fernet(_derive_fernet_key(get_settings().credential_encryption_key))


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
