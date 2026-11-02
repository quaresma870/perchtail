import time

import pyotp
import pytest
from app.auth import mfa
from app.auth.models import MfaBackupCode, Role
from app.auth.providers.local import create_local_user
from sqlmodel import select


def _make_role(session) -> Role:
    role = Role(name="Support")
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def _make_user(session, username="jdoe@example.com"):
    role = _make_role(session)
    return create_local_user(
        session, actor_user_id=None, username=username, password="s3cret!", role_id=role.id
    )


def test_start_enrollment_sets_pending_secret_without_enabling_mfa(session):
    user = _make_user(session)

    secret = mfa.start_enrollment(session, user)

    assert user.mfa_enabled is False
    assert user.mfa_secret_encrypted is not None
    assert secret  # a real base32 secret was returned to the caller


def test_start_enrollment_called_twice_replaces_the_pending_secret(session):
    user = _make_user(session)

    first_secret = mfa.start_enrollment(session, user)
    second_secret = mfa.start_enrollment(session, user)

    assert first_secret != second_secret
    # Only the second secret's code should now verify.
    assert mfa.verify_totp_code(second_secret, pyotp.TOTP(second_secret).now())
    assert not mfa.verify_totp_code(first_secret, pyotp.TOTP(second_secret).now())


def test_confirm_enrollment_with_valid_code_enables_mfa_and_returns_backup_codes(session):
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    code = pyotp.TOTP(secret).now()

    codes = mfa.confirm_enrollment(session, user, code)

    assert user.mfa_enabled is True
    assert user.mfa_enrolled_at is not None
    assert len(codes) == mfa.BACKUP_CODE_COUNT
    assert len(set(codes)) == mfa.BACKUP_CODE_COUNT  # all distinct

    stored = session.exec(select(MfaBackupCode).where(MfaBackupCode.user_id == user.id)).all()
    assert len(stored) == mfa.BACKUP_CODE_COUNT
    assert all(row.used_at is None for row in stored)


def test_confirm_enrollment_with_invalid_code_raises_and_does_not_enable_mfa(session):
    user = _make_user(session)
    mfa.start_enrollment(session, user)

    with pytest.raises(ValueError):
        mfa.confirm_enrollment(session, user, "000000")

    assert user.mfa_enabled is False


def test_confirm_enrollment_without_a_pending_secret_raises(session):
    user = _make_user(session)

    with pytest.raises(ValueError):
        mfa.confirm_enrollment(session, user, "123456")


def test_verify_mfa_code_accepts_a_valid_totp_code(session):
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    mfa.confirm_enrollment(session, user, pyotp.TOTP(secret).now())

    # Confirming already consumed the current 30s step (see
    # test_verify_mfa_code_rejects_a_replayed_totp_code below) -- use the
    # next step's code, same as a real login attempt a few seconds later
    # would present.
    next_code = pyotp.TOTP(secret).at(int(time.time()) + 30)
    assert mfa.verify_mfa_code(session, user, next_code) is True


def test_verify_mfa_code_rejects_a_replayed_totp_code(session):
    """A single observed code must not work twice, even though pyotp's own
    verify() would happily accept it again within its ~90s window --
    User.mfa_last_used_step is what makes a live code single-use."""
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    code = pyotp.TOTP(secret).now()
    mfa.confirm_enrollment(session, user, code)

    assert mfa.verify_mfa_code(session, user, code) is False


def test_verify_mfa_code_rejects_a_wrong_code(session):
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    mfa.confirm_enrollment(session, user, pyotp.TOTP(secret).now())

    assert mfa.verify_mfa_code(session, user, "000000") is False


def test_verify_mfa_code_returns_false_when_mfa_is_not_enabled(session):
    user = _make_user(session)
    assert mfa.verify_mfa_code(session, user, "123456") is False


def test_verify_mfa_code_accepts_and_consumes_a_backup_code(session):
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    codes = mfa.confirm_enrollment(session, user, pyotp.TOTP(secret).now())

    assert mfa.verify_mfa_code(session, user, codes[0]) is True

    row = session.exec(select(MfaBackupCode).where(MfaBackupCode.user_id == user.id)).all()
    used = [r for r in row if r.used_at is not None]
    assert len(used) == 1


def test_a_backup_code_cannot_be_used_twice(session):
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    codes = mfa.confirm_enrollment(session, user, pyotp.TOTP(secret).now())

    assert mfa.verify_mfa_code(session, user, codes[0]) is True
    assert mfa.verify_mfa_code(session, user, codes[0]) is False


def test_backup_code_matching_is_case_and_dash_insensitive(session):
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    codes = mfa.confirm_enrollment(session, user, pyotp.TOTP(secret).now())

    mangled = codes[0].upper().replace("-", " ")
    assert mfa.verify_mfa_code(session, user, mangled) is True


def test_disable_mfa_clears_secret_and_deletes_backup_codes(session):
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    mfa.confirm_enrollment(session, user, pyotp.TOTP(secret).now())

    mfa.disable_mfa(session, user)

    assert user.mfa_enabled is False
    assert user.mfa_secret_encrypted is None
    assert user.mfa_enrolled_at is None
    remaining = session.exec(select(MfaBackupCode).where(MfaBackupCode.user_id == user.id)).all()
    assert remaining == []


def test_regenerate_backup_codes_replaces_the_old_set(session):
    user = _make_user(session)
    secret = mfa.start_enrollment(session, user)
    old_codes = mfa.confirm_enrollment(session, user, pyotp.TOTP(secret).now())

    new_codes = mfa.regenerate_backup_codes(session, user)

    assert set(new_codes).isdisjoint(old_codes)
    # The old codes no longer verify -- only the new set does.
    assert mfa.verify_mfa_code(session, user, old_codes[0]) is False
    assert mfa.verify_mfa_code(session, user, new_codes[0]) is True


def test_regenerate_backup_codes_raises_when_mfa_is_not_enabled(session):
    user = _make_user(session)

    with pytest.raises(ValueError):
        mfa.regenerate_backup_codes(session, user)


def test_verify_totp_code_rejects_non_numeric_input():
    secret = mfa.generate_totp_secret()
    assert mfa.verify_totp_code(secret, "not-a-code") is False


def test_provisioning_uri_includes_issuer_and_username():
    secret = mfa.generate_totp_secret()
    uri = mfa.provisioning_uri(secret, "jdoe@example.com")
    assert "PerchTail" in uri
    assert "jdoe%40example.com" in uri
