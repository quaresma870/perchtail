"""Optional TOTP-based multi-factor authentication for local accounts
(ROADMAP.md's Security hardening section) -- SSO already delegates a second
factor to the IdP, so this exists only for auth_provider=local: the local
break-glass account, and any deployment that doesn't enable SSO at all.

Enrollment is two calls, never one: start_enrollment persists a pending
secret (mfa_enabled still False), and confirm_enrollment only flips it on
once the caller proves they can actually produce a valid code with it --
otherwise a typo'd QR-code scan could silently lock a user out with a
secret their authenticator app never actually holds.

None of the state-mutating functions below commit on their own (aside from
start_enrollment, which has no paired audit event to stay atomic with) --
each is called from an API endpoint that adds its own AuditLog entry
afterward and commits once, so the security-state change and its audit
record land in the same transaction. See app/audit.py's record_audit_event
docstring for the same convention.
"""

import secrets
import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlmodel import Session, select

from app.auth.models import MfaBackupCode, User
from app.crypto import decrypt_secret, encrypt_secret
from app.timeutils import utcnow

_hasher = PasswordHasher()

TOTP_ISSUER = "PerchTail"
BACKUP_CODE_COUNT = 10
_TOTP_INTERVAL_SECONDS = 30
_TOTP_VALID_WINDOW = 1  # tolerates one 30s step of clock drift either way


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    """An otpauth:// URI an authenticator app can render as a QR code --
    encodes the secret, the account label, and the issuer so the app shows
    "PerchTail (username)" rather than a bare, unlabeled entry."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def verify_totp_code(secret: str, code: str) -> bool:
    """A stateless, non-consuming check of whether `code` is currently valid
    for `secret` -- used only where there's no User row to track replay
    against (tests, and comparing two secrets against each other). Real
    login/enrollment paths must go through _consume_totp_code instead, which
    additionally rejects a code already used for a prior step."""
    normalized = code.strip().replace(" ", "")
    if not normalized.isdigit():
        return False
    return pyotp.TOTP(secret).verify(normalized, valid_window=_TOTP_VALID_WINDOW)


def _consume_totp_code(session: Session, user: User, secret: str, code: str) -> bool:
    """Like verify_totp_code, but single-use: a code is only accepted if its
    time-step counter is strictly newer than the last one this user
    successfully used (User.mfa_last_used_step). Without this, pyotp's own
    verify() happily accepts the same still-valid code more than once within
    its ~90s window -- a shoulder-surfed or proxy-logged code could
    otherwise be replayed, unlike a backup code, which MfaBackupCode.used_at
    already makes single-use."""
    normalized = code.strip().replace(" ", "")
    if not normalized.isdigit():
        return False

    totp = pyotp.TOTP(secret)
    now = time.time()
    current_counter = int(now // _TOTP_INTERVAL_SECONDS)
    for offset in range(-_TOTP_VALID_WINDOW, _TOTP_VALID_WINDOW + 1):
        # counter_offset shifts by whole time-steps relative to `now` --
        # NOT the same as TOTP.at(<a raw counter value>), which instead
        # treats its argument as a timestamp.
        if not secrets.compare_digest(totp.at(now, counter_offset=offset), normalized):
            continue
        counter = current_counter + offset
        if user.mfa_last_used_step is not None and counter <= user.mfa_last_used_step:
            return False  # a previously-accepted step being replayed
        user.mfa_last_used_step = counter
        session.add(user)
        return True
    return False


def _normalize_backup_code(code: str) -> str:
    return code.strip().replace("-", "").replace(" ", "").lower()


def _format_backup_code(raw_hex: str) -> str:
    return f"{raw_hex[:5]}-{raw_hex[5:]}"


def generate_backup_codes() -> list[str]:
    return [_format_backup_code(secrets.token_hex(5)) for _ in range(BACKUP_CODE_COUNT)]


def hash_backup_code(code: str) -> str:
    return _hasher.hash(_normalize_backup_code(code))


def _replace_backup_codes(session: Session, user: User) -> list[str]:
    """Deletes every existing code (used or not) and issues a fresh set --
    used by both confirm_enrollment and regenerate_backup_codes, since a
    previous partial set is never worth keeping once new ones are issued."""
    existing = session.exec(select(MfaBackupCode).where(MfaBackupCode.user_id == user.id)).all()
    for row in existing:
        session.delete(row)

    codes = generate_backup_codes()
    for plaintext in codes:
        session.add(MfaBackupCode(user_id=user.id, code_hash=hash_backup_code(plaintext)))
    return codes


def start_enrollment(session: Session, user: User) -> str:
    """Generates and persists a new pending TOTP secret. Does not enable MFA
    -- see confirm_enrollment. The caller (POST /auth/mfa/enroll) is
    responsible for rejecting this while MFA is already enabled -- calling
    it mid-flow before that would silently replace the active secret and
    break the user's already-working authenticator before they've confirmed
    a replacement can actually work."""
    secret = generate_totp_secret()
    user.mfa_secret_encrypted = encrypt_secret(secret)
    session.add(user)
    session.commit()
    session.refresh(user)
    return secret


def confirm_enrollment(session: Session, user: User, code: str) -> list[str]:
    """Verifies the pending secret with a real code, then turns MFA on and
    issues a fresh set of backup codes, returned in plaintext exactly once
    here -- only their argon2 hashes are ever stored. Raises ValueError if
    there's no pending secret or the code doesn't verify (caller maps that
    to a 400/401 at the API layer). Does not commit -- see module docstring."""
    if user.mfa_secret_encrypted is None:
        raise ValueError("no pending MFA enrollment")

    secret = decrypt_secret(user.mfa_secret_encrypted)
    if not _consume_totp_code(session, user, secret, code):
        raise ValueError("invalid code")

    user.mfa_enabled = True
    user.mfa_enrolled_at = utcnow()
    session.add(user)
    codes = _replace_backup_codes(session, user)
    return codes


def disable_mfa(session: Session, user: User) -> None:
    """Does not commit -- see module docstring."""
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.mfa_enrolled_at = None
    user.mfa_last_used_step = None
    session.add(user)

    existing = session.exec(select(MfaBackupCode).where(MfaBackupCode.user_id == user.id)).all()
    for row in existing:
        session.delete(row)


def regenerate_backup_codes(session: Session, user: User) -> list[str]:
    """Raises ValueError if MFA isn't enabled -- there's nothing to
    regenerate codes for. Does not commit -- see module docstring."""
    if not user.mfa_enabled:
        raise ValueError("MFA is not enabled")
    return _replace_backup_codes(session, user)


def _consume_backup_code(session: Session, user: User, code: str) -> bool:
    normalized = _normalize_backup_code(code)
    if not normalized:
        return False

    candidates = session.exec(
        select(MfaBackupCode).where(
            MfaBackupCode.user_id == user.id, MfaBackupCode.used_at.is_(None)
        )
    ).all()
    for candidate in candidates:
        try:
            _hasher.verify(candidate.code_hash, normalized)
        except VerifyMismatchError:
            continue
        candidate.used_at = utcnow()
        session.add(candidate)
        return True
    return False


def verify_mfa_code(session: Session, user: User, code: str) -> bool:
    """Used at login time only -- tries the code as a live, not-yet-used TOTP
    code first, then as a one-time backup code (consuming it on match).
    Enrollment itself calls _consume_totp_code directly against the
    still-pending secret, since a backup code can't exist yet at that point.
    Does not commit -- the caller (POST /auth/login) bundles this with its
    own login-finalization commit via complete_login."""
    if not user.mfa_enabled or user.mfa_secret_encrypted is None:
        return False

    secret = decrypt_secret(user.mfa_secret_encrypted)
    if _consume_totp_code(session, user, secret, code):
        return True
    return _consume_backup_code(session, user, code)
