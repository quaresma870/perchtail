"""Which local accounts must use MFA, and which count as emergency accounts.

MFA is always required of a local account whose role is a super admin or
holds any global capability: those accounts administer the deployment, and
a password alone isn't enough for them. With the system setting
`require_mfa_local_accounts` (Settings -> Sign-in security, off by default), every
other local account needs it too. SSO accounts are never covered: their
second factor belongs to the identity provider.

An account that must use MFA and hasn't set it up can sign in, but until it
does, the API answers everything except setting it up (and the few calls
the forced password change also allows) with 403 `mfa_setup_required`
(app/api/auth.py, get_current_active_user).
"""

from sqlmodel import Session

from app import system_settings
from app.auth.models import AuthProviderType, User

SETTING = "require_mfa_local_accounts"

# Tests that don't exercise MFA turn enforcement off (tests/conftest.py), as
# app.source_status.ENABLED does for status writes. Always True in the app.
ENFORCED = True


def admin_account(user: User) -> bool:
    """A super admin, or a role with any global capability."""
    return bool(user.role.is_super_admin or user.role.global_capabilities)


def mfa_required(session: Session, user: User) -> bool:
    if not ENFORCED or user.auth_provider != AuthProviderType.local:
        return False
    if admin_account(user):
        return True
    return system_settings.get_bool(session, SETTING)


def mfa_setup_required(session: Session, user: User) -> bool:
    """Covered by the requirement, and MFA isn't on yet."""
    return not user.mfa_enabled and mfa_required(session, user)


def emergency_account(user: User) -> bool:
    """An active local super admin with MFA on: the break-glass account(s).
    Their failed logins are counted per address as well
    (app/login_throttle.py), so an anonymous caller can't lock them out
    everywhere; MFA is what stands against guessing their password from many
    addresses. Until MFA is on, a super admin is locked per username like
    any other account: otherwise a password guessed from many addresses
    would let an attacker set up their own second factor on it."""
    return bool(
        user.active
        and user.auth_provider == AuthProviderType.local
        and user.role.is_super_admin
        and user.mfa_enabled
    )
