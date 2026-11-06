from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlmodel import Session, select

from app import password_policy
from app.audit import record_audit_event
from app.auth.models import AuthProviderType, User
from app.logging_config import get_logger
from app.timeutils import utcnow

logger = get_logger(__name__)
_hasher = PasswordHasher()
# Verified against when there's no real hash to check (unknown, inactive or
# SSO-only account), so every failed login costs the same argon2 work and the
# response time doesn't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("perchtail-timing-equalizer")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_hash(stored_hash: str, password: str) -> bool:
    try:
        _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def verify_password(user: User, password: str) -> bool:
    if user.password_hash is None:
        return False
    return verify_hash(user.password_hash, password)


def _burn_a_verify(password: str) -> None:
    try:
        _hasher.verify(_DUMMY_HASH, password)
    except VerifyMismatchError:
        pass


def create_local_user(
    session: Session, *, actor_user_id: int | None, username: str, password: str, role_id: int
) -> User:
    """Admin-created local account. Starts with must_change_password=True per
    CLAUDE.md's Security notes ("Force password change on first login for
    admin-created local accounts"). The caller checks the password against
    the policy (the users API does; the first-run seed generates one)."""
    user = User(
        username=username,
        password_hash=hash_password(password),
        role_id=role_id,
        auth_provider=AuthProviderType.local,
        must_change_password=True,
        password_changed_at=utcnow(),
    )
    session.add(user)
    session.flush()

    record_audit_event(
        session,
        user_id=actor_user_id,
        action="user.create",
        target_type="user",
        target_id=user.id,
        metadata={"username": username},
    )
    session.commit()
    session.refresh(user)
    return user


def set_password(
    session: Session,
    user: User,
    new_password: str,
    policy: password_policy.PasswordPolicy | None = None,
) -> None:
    """Replaces the account's password hash, keeping the outgoing one in the
    history when the policy needs it. Doesn't check the policy or commit."""
    policy = policy or password_policy.get_policy(session)
    password_policy.remember_previous(session, user, policy)
    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    session.add(user)


def change_password(session: Session, user: User, new_password: str) -> None:
    """The user's own change (also the forced one at first login or after
    expiry). Raises PasswordPolicyError, before changing anything, when the
    password breaks the policy (#245). Doesn't commit: the caller commits
    it together with revoking the other sessions."""
    policy = password_policy.get_policy(session)
    password_policy.check_password(
        new_password, policy, user_identifiers=password_policy.user_identifiers(user)
    )
    # Whatever the history rule says, a forced change (first login, reset,
    # expiry) mustn't be satisfied by entering the same password again.
    if user.password_hash and verify_hash(user.password_hash, new_password):
        raise password_policy.PasswordPolicyError(
            "same_as_current", "Choose a password different from the current one."
        )
    password_policy.check_history(session, user, new_password, policy)
    set_password(session, user, new_password, policy)
    user.must_change_password = False
    session.add(user)

    record_audit_event(
        session,
        user_id=user.id,
        action="user.change_password",
        target_type="user",
        target_id=user.id,
    )


class LocalPasswordProvider:
    """Local username/password auth — always present, the break-glass
    super-admin path (see CLAUDE.md's Access control section)."""

    def authenticate(self, session: Session, username: str, password: str) -> User | None:
        """Verifies credentials only — does NOT finalize the sign-in (see
        complete_login below). A user with MFA enabled still has to clear a
        second factor before this counts as a successful login; committing
        last_login_at or a user.login audit event here would misrepresent
        an only-half-completed attempt as a real one."""
        user = session.exec(select(User).where(User.username == username)).first()
        if user is None or not user.active or user.password_hash is None:
            _burn_a_verify(password)
            return None

        if not verify_password(user, password):
            return None

        if _hasher.check_needs_rehash(user.password_hash):
            user.password_hash = _hasher.hash(password)
            session.add(user)
            session.commit()
            session.refresh(user)

        return user


def complete_login(session: Session, user: User) -> None:
    """Finalizes a successful sign-in — sets last_login_at and records the
    user.login audit event. Called once every required factor (password,
    and MFA if user.mfa_enabled) has verified; see api/auth.py's login
    endpoint for where this sits in the flow."""
    user.last_login_at = utcnow()
    session.add(user)

    record_audit_event(
        session,
        user_id=user.id,
        action="user.login",
        target_type="user",
        target_id=user.id,
    )
    session.commit()
    session.refresh(user)
