"""How strong CREDENTIAL_ENCRYPTION_KEY is, checked at startup.

The key is any string, stretched with PBKDF2 and the per-install salt
(app/crypto.py), so a short or guessable key stays the weak point however
many iterations it gets. A key is strong enough when both hold:

- **At least 32 bytes once decoded.** A key written as hex (an even number
  of hex digits) or as base64 / base64url (with or without padding) counts
  the bytes it decodes to; any other key counts its UTF-8 bytes. The
  documented generators all give 32 bytes: `secrets.token_urlsafe(32)`,
  `openssl rand -base64 32` and `openssl rand -hex 32`.
- **At least 128 bits of estimated entropy**: the key's length times the
  Shannon entropy of its own characters, capped by the size of the key
  compressed with zlib (8 bits per byte, without zlib's fixed overhead).
  The first catches a key made of a few characters; the cap catches a
  repeated word or phrase (`"changeme" * 6`), whose characters alone look
  varied, so neither passes on length alone.

In this release a weak key is a warning (the log at startup and Settings ->
System); `CREDENTIAL_KEY_REFUSE_WEAK=true` turns it into a refusal to start,
which is meant to become the default in 1.0.
"""

import base64
import binascii
import math
import re
import zlib
from collections import Counter

MIN_KEY_BYTES = 32
MIN_ENTROPY_BITS = 128

_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]+={0,2}")

WEAK_KEY_ADVICE = (
    "Generate a new key, for example with "
    'python3 -c "import secrets; print(secrets.token_urlsafe(32))", and change to it '
    "with python -m app.rotate_credential_key (docs/credential-key-rotation.md): "
    "replacing the value alone makes the stored credentials unreadable."
)


def decoded_key_bytes(key: str) -> bytes:
    """The key's bytes: decoded from hex or base64 when it is written in one
    of them, else its UTF-8 encoding."""
    text = key.strip()
    if _HEX.fullmatch(text):
        return bytes.fromhex(text)
    if _BASE64.fullmatch(text) and len(text.rstrip("=")) % 4 != 1:
        standard = text.rstrip("=").replace("-", "+").replace("_", "/")
        try:
            return base64.b64decode(standard + "=" * (-len(standard) % 4), validate=True)
        except (binascii.Error, ValueError):
            pass
    return text.encode("utf-8")


_ZLIB_OVERHEAD = len(zlib.compress(b"", 9))


def entropy_bits(key: str) -> float:
    """Length times the Shannon entropy of the key's own characters, capped
    by the key's zlib-compressed size in bits, which sees repetition."""
    if not key:
        return 0.0
    counts = Counter(key)
    total = len(key)
    per_char = -sum((n / total) * math.log2(n / total) for n in counts.values())
    compressed = max(len(zlib.compress(key.encode("utf-8"), 9)) - _ZLIB_OVERHEAD, 0) * 8
    return min(per_char * total, float(compressed))


def is_weak(key: str) -> bool:
    return bool(weaknesses(key))


def weaknesses(key: str) -> list[str]:
    """What makes the key weak, in words, with its length and estimated
    bits: for the operator's startup log only. Anything shown in the app
    says only that the key is weak (is_weak), since these numbers narrow the
    search for the secret everything depends on. Empty when it is strong
    enough."""
    found: list[str] = []
    length = len(decoded_key_bytes(key))
    if length < MIN_KEY_BYTES:
        found.append(f"it is {length} bytes long; at least {MIN_KEY_BYTES} are needed")
    bits = entropy_bits(key.strip())
    if bits < MIN_ENTROPY_BITS:
        found.append(
            f"it has about {int(bits)} bits of entropy; at least {MIN_ENTROPY_BITS} are needed"
        )
    return found
