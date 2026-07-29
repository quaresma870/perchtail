import base64
import hashlib
import json
from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


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
