import math
import secrets
from typing import NoReturn

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app import (
    auth_events,
    password_policy,
    password_reset_limits,
    preferences,
    profile,
    request_context,
)
from app.audit import record_audit_event
from app.auth import mfa, mfa_policy, password_reset
from app.auth.models import (
    AuthProviderType,
    AuthSession,
    GlobalCapability,
    Role,
    SSOProviderConfig,
    User,
)
from app.auth.providers.local import (
    LocalPasswordProvider,
    change_password,
    complete_login,
    verify_password,
)
from app.auth.providers.oidc import (
    STATE_TTL_SECONDS,
    InsecureIdPURLError,
    IssuerMismatchError,
    OIDCProvider,
    decode_state,
    encode_state,
    generate_code_verifier,
    parse_oidc_settings,
    read_provider_config,
)
from app.auth.rbac import forbidden
from app.auth.sessions import (
    create_session,
    delete_session,
    get_user_by_token,
    hash_token,
    list_sessions_for_user,
    replace_session,
    revoke_all_sessions,
    revoke_session,
)
from app.bootstrap import NO_ACCESS_ROLE_NAME
from app.config import get_settings
from app.db import get_session
from app.initial_admin_password import remove_initial_admin_password
from app.logging_config import get_logger
from app.login_throttle import (
    FailureOutcome,
    record_failure,
    record_success,
    seconds_until_allowed,
    sso_login_from_address,
)
from app.timeutils import utcnow

logger = get_logger(__name__)

SESSION_COOKIE_NAME = "perchtail_session"
# Binds the OAuth `state` param to the browser that started the SSO flow --
# see sso_login/sso_callback below and issue #61. Lax, not Strict: it must
# still be sent on the top-level GET navigation the IdP uses to redirect
# back to /auth/sso/callback, which is cross-site from the cookie's own
# origin's perspective (Strict would never attach it there at all).
#
# One cookie, not a set -- a second concurrent /auth/sso/login call in the
# same browser (a second tab, a double-click) overwrites it, so finishing
# an earlier flow afterward fails this check and the user just retries.
# Supporting truly concurrent flows would need a per-flow cookie name or a
# list of valid states; not worth that complexity for what's normally a
# single linear login action.
SSO_STATE_COOKIE_NAME = "perchtail_sso_state"

# With Secure cookies (the default), both names carry the __Host- prefix: a
# browser then only accepts them from this host, over HTTPS, with Path=/ and
# no Domain, so neither a sibling subdomain nor a plain-HTTP page can set or
# overwrite them. Without Secure (SESSION_COOKIE_SECURE=false) a browser
# refuses __Host- names, so the plain names above stay. A cookie under the
# other name is ignored: everyone signs in once after the change.
HOST_PREFIX = "__Host-"


def session_cookie_name() -> str:
    """The session cookie's name for this deployment."""
    if get_settings().session_cookie_secure:
        return HOST_PREFIX + SESSION_COOKIE_NAME
    return SESSION_COOKIE_NAME


def sso_state_cookie_name() -> str:
    """The SSO state cookie's name for this deployment."""
    if get_settings().session_cookie_secure:
        return HOST_PREFIX + SSO_STATE_COOKIE_NAME
    return SSO_STATE_COOKIE_NAME


def session_cookie(request: Request) -> str | None:
    """The session token the request carries, under this deployment's name."""
    return request.cookies.get(session_cookie_name())


def read_sso_state_cookie(request: Request) -> str | None:
    return request.cookies.get(sso_state_cookie_name())


router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    # Only required when the account has MFA enabled -- accepts either a
    # live 6-digit TOTP code or a backup code (see auth/mfa.py's
    # verify_mfa_code, which tries both).
    mfa_code: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def password_policy_error(exc: password_policy.PasswordPolicyError) -> HTTPException:
    """The 422 for a password the policy refuses (#245): the rule's message,
    never the password."""
    return HTTPException(
        status_code=422,
        detail={"message": str(exc), "error_code": f"password_{exc.rule}"},
    )


def password_length_limit(session: Session) -> int:
    """The longest password a request may carry: the policy's maximum, or
    the ceiling of that setting when it has none."""
    limit = password_policy.get_policy(session).max_length
    if not limit or limit > password_policy.MAX_LENGTH_CEILING:
        return password_policy.MAX_LENGTH_CEILING
    return limit


def require_password_length(session: Session, *passwords: str | None) -> None:
    """Refuses a password longer than password_length_limit before anything
    hashes or checks it, so a huge body costs neither argon2 time nor the
    policy's checks. The 422 never quotes the password."""
    limit = password_length_limit(session)
    if any(p is not None and len(p) > limit for p in passwords):
        raise password_policy_error(
            password_policy.PasswordPolicyError("too_long", f"Use at most {limit} characters.")
        )


class PreferencesUpdate(BaseModel):
    """PATCH /auth/preferences: a value sets the choice, null clears it back
    to the System default, an omitted key is left as it is."""

    explorer_sort: str | None = None
    connections_view: str | None = None


class ProfileUpdate(BaseModel):
    """PATCH /auth/me: an omitted field is left as it is; an empty string
    clears it. Changing the email needs the current password, since the
    email is where a password-reset link goes (#244)."""

    display_name: str | None = None
    email: str | None = None
    current_password: str | None = None


def profile_error(exc: profile.ProfileError) -> HTTPException:
    """409 for an email another account has, 422 for anything malformed."""
    code = 409 if isinstance(exc, profile.EmailTakenError) else 422
    return HTTPException(status_code=code, detail=str(exc))


class PasswordPolicyPublic(BaseModel):
    min_length: int
    max_length: int
    reject_simple: bool
    reject_personal: bool
    history: int
    required_classes: int
    max_age_days: int


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    # My profile (#243). For an SSO account (auth_provider other than
    # local) they come from the IdP and are read-only.
    display_name: str | None
    email: str | None
    auth_provider: AuthProviderType
    # The display choices this user made (#273); anything missing follows
    # the System default (explorer_sort_default, connections_view_default).
    preferences: dict[str, str]
    role_id: int
    active: bool
    must_change_password: bool
    mfa_enabled: bool
    # Whether this account must use MFA (app/auth/mfa_policy.py): while it's
    # true and mfa_enabled isn't, the only thing it can do is set MFA up.
    mfa_required: bool
    # Denormalized from the user's role so the frontend can gate nav/UI
    # (e.g. show the Roles/Users admin pages) without a second round-trip or
    # needing manage_roles just to read its own role's shape.
    is_super_admin: bool
    global_capabilities: list[GlobalCapability]

    @classmethod
    def from_user(cls, user: User, session: Session) -> "UserPublic":
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            auth_provider=user.auth_provider,
            preferences=preferences.own_choices(user),
            role_id=user.role_id,
            active=user.active,
            must_change_password=user.must_change_password,
            mfa_enabled=user.mfa_enabled,
            mfa_required=mfa_policy.mfa_required(session, user),
            is_super_admin=user.role.is_super_admin,
            global_capabilities=user.role.global_capabilities,
        )


# Sent by the web interface on the requests it makes on its own (the
# Explorer's source list and status, refreshing open folders, the progress of
# an open): they keep working but don't count as use for the idle timeout.
BACKGROUND_HEADER = "X-PerchTail-Background"


def get_current_user(
    session: Session = Depends(get_session),
    session_token: str | None = Depends(session_cookie),
    background: str | None = Header(default=None, alias=BACKGROUND_HEADER),
) -> User:
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user = get_user_by_token(session, session_token, count_as_use=background != "1")
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    # Every audit entry this request writes names its session, and a
    # refusal names its user (app/request_context.py).
    context = request_context.current()
    if context is not None:
        context.session_id = request_context.session_ref(hash_token(session_token))
        context.user_id = user.id
    return user


def _not_a_denial() -> None:
    # Not a refused permission (app/api/errors.py): the account just has a
    # step to finish first, and every screen asks until it does.
    context = request_context.current()
    if context is not None:
        context.denial_handled = True


def get_current_user_setting_up_mfa(user: User = Depends(get_current_user)) -> User:
    """For the MFA setup calls: blocks only until a forced password change
    is done, so an account that must use MFA can still set it up."""
    if user.must_change_password:
        _not_a_denial()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Password change required"
        )
    return user


MFA_SETUP_REQUIRED = "mfa_setup_required"


def get_current_active_user(
    user: User = Depends(get_current_user_setting_up_mfa),
    session: Session = Depends(get_session),
) -> User:
    """Blocks every endpoint except login/me/change-password until an
    admin-created account's forced password change is done, and every
    endpoint except those and the MFA setup until an account that must use
    MFA (app/auth/mfa_policy.py) has set it up."""
    if mfa_policy.mfa_setup_required(session, user):
        _not_a_denial()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Set up two-factor authentication to continue",
                "error_code": MFA_SETUP_REQUIRED,
            },
        )
    return user


def require_super_admin(user: User = Depends(get_current_active_user)) -> User:
    """For endpoints only a super admin may use (config import/export,
    recording known audit-chain breaks)."""
    if not user.role.is_super_admin:
        raise forbidden("Super admins only", reason="super_admin_required", user=user)
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=session_cookie_name(),
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


def _clear_sso_state_cookie(response: Response) -> None:
    # Match the attributes it was set with (see sso_login) -- Response
    # .delete_cookie()'s own defaults (secure=False) wouldn't necessarily
    # clear a cookie that was set with secure=True.
    settings = get_settings()
    response.delete_cookie(
        sso_state_cookie_name(),
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _per_address_account(session: Session, username: str) -> bool:
    """Whether this username's failed logins are counted per client address
    (an emergency account, app/auth/mfa_policy.py), so an anonymous caller
    can't lock it out everywhere (app/login_throttle.py)."""
    user = session.exec(select(User).where(User.username == username)).first()
    return user is not None and mfa_policy.emergency_account(user)


def _record_failed_login(
    session: Session,
    username: str,
    client_ip: str | None,
    reason: str,
    *,
    per_address_account: bool,
) -> None:
    """Counts the failure toward the lockouts, audits it, and audits each
    lockout this failure triggers."""
    outcome = record_failure(username, client_ip, per_address_account=per_address_account)
    auth_events.login_failed(session, username=username, client_ip=client_ip, reason=reason)
    _audit_lockouts(session, username, client_ip, outcome, per_address_account=per_address_account)


def _audit_lockouts(
    session: Session,
    username: str,
    client_ip: str | None,
    outcome: FailureOutcome,
    *,
    per_address_account: bool,
) -> None:
    """Audits each lockout a failure triggered (app/login_throttle.py), one
    user.locked_out per scope -- after a failed login or a failed password
    re-confirmation alike, since both count toward the same lockouts."""
    lock_seconds = get_settings().login_lockout_seconds
    if outcome.account_locked:
        auth_events.locked_out(
            session,
            username=username,
            client_ip=client_ip,
            lock_seconds=lock_seconds,
            scope="account_and_address" if per_address_account else "account",
        )
    if outcome.address_locked:
        auth_events.locked_out(
            session,
            username=username,
            client_ip=client_ip,
            lock_seconds=lock_seconds,
            scope="address",
        )


def _too_many_attempts(remaining: float, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(math.ceil(remaining))},
    )


@router.post("/login", response_model=UserPublic)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session_token: str | None = Depends(session_cookie),
    session: Session = Depends(get_session),
):
    require_password_length(session, payload.password)
    client_ip = request.client.host if request.client else None
    per_address = _per_address_account(session, payload.username)
    remaining = seconds_until_allowed(payload.username, client_ip, per_address_account=per_address)
    if remaining is not None:
        auth_events.login_throttled(session, username=payload.username, client_ip=client_ip)
        raise _too_many_attempts(remaining, "Too many failed login attempts. Try again later.")

    def failed(reason: str) -> None:
        _record_failed_login(
            session, payload.username, client_ip, reason, per_address_account=per_address
        )

    user = LocalPasswordProvider().authenticate(session, payload.username, payload.password)
    if user is None:
        failed("bad_credentials")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user.mfa_enabled:
        # A missing code isn't a guess that failed -- it's the client
        # correctly stopping after step one to ask the user for a code, so
        # it doesn't count against the login-throttle lockout the way a
        # present-but-wrong code below does.
        if not payload.mfa_code:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "message": "Authentication code required",
                    "error_code": "mfa_required",
                },
            )
        if not mfa.verify_mfa_code(session, user, payload.mfa_code):
            # Whatever the check recorded (user.mfa_secret_unreadable) is
            # kept, even when the failure below is folded into an earlier one.
            session.commit()
            failed("mfa_invalid_code")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "message": "Invalid authentication code",
                    "error_code": "mfa_invalid_code",
                },
            )

    record_success(payload.username, client_ip, per_address_account=per_address)
    # Expired password (#245): signed in, but only to change it.
    if password_policy.password_expired(user, password_policy.get_policy(session)):
        user.must_change_password = True
        logger.info("user.password_expired", user_id=user.id)
    # Signed in, but only to set MFA up (app/auth/mfa_policy.py).
    if mfa_policy.mfa_setup_required(session, user):
        record_audit_event(
            session,
            user_id=user.id,
            action="user.mfa_required",
            target_type="user",
            target_id=user.id,
            metadata={"reason": "admin_account" if mfa_policy.admin_account(user) else "setting"},
        )
    complete_login(session, user)
    # The session this browser had, whoever it was for, ends with this
    # sign-in rather than linger until it expires.
    if session_token:
        end_previous_session(session, session_token, signed_in=user)
    _, token = create_session(session, user, user_agent=request.headers.get("user-agent"))
    _set_session_cookie(response, token)
    return UserPublic.from_user(user, session)


def end_previous_session(session: Session, token: str, *, signed_in: User) -> None:
    """Ends the session a browser still had when someone signs in with it
    (locally or through SSO). A session that hadn't expired yet is audited
    as session.revoke, reason signed_in_again, against its own user: it may
    have been someone else's."""
    previous = session.exec(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).first()
    if previous is None:
        return
    if previous.expires_at >= utcnow():
        record_audit_event(
            session,
            user_id=signed_in.id,
            action="session.revoke",
            target_type="user",
            target_id=previous.user_id,
            metadata={"count": 1, "reason": "signed_in_again"},
        )
    session.delete(previous)
    session.commit()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    session: Session = Depends(get_session),
    session_token: str | None = Depends(session_cookie),
):
    if session_token is not None:
        # Audited only for a session that was still valid: a stale cookie
        # signs no one out.
        user = get_user_by_token(session, session_token)
        if user is not None:
            context = request_context.current()
            if context is not None:
                context.session_id = request_context.session_ref(hash_token(session_token))
            record_audit_event(
                session,
                user_id=user.id,
                action="user.logout",
                target_type="user",
                target_id=user.id,
            )
            session.commit()
        delete_session(session, session_token)
    settings = get_settings()
    # With the attributes it was set with: a browser drops a __Host- cookie
    # only from a Set-Cookie that is Secure, with Path=/.
    response.delete_cookie(
        session_cookie_name(),
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    # The browser's cache of this site goes too: pages and answers the user
    # saw stay out of reach of the next person at the same browser.
    response.headers["Clear-Site-Data"] = '"cache"'


@router.get("/me", response_model=UserPublic)
def me(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return UserPublic.from_user(user, session)


@router.post("/change-password", response_model=UserPublic)
def change_password_endpoint(
    payload: ChangePasswordRequest,
    response: Response,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    session_token: str | None = Depends(session_cookie),
):
    require_password_length(session, payload.current_password, payload.new_password)
    # Throttled like /auth/login, so a stolen session can't be used to
    # brute-force the account password at argon2 speed.
    _require_password_reconfirmation(
        session, user, payload.current_password, action="change_password"
    )

    try:
        change_password(session, user, payload.new_password)
    except password_policy.PasswordPolicyError as exc:
        session.rollback()
        raise password_policy_error(exc) from exc
    # Every other session was opened with the old password; it shouldn't
    # outlive it. This one (which just proved the password) goes on under a
    # new token, so a token copied before the change stops working too.
    # One commit, so the new password never lands without the revocation.
    _end_other_sessions(session, user, session_token, reason="password_change")
    new_token = replace_session(session, user, session_token)
    session.commit()
    _set_session_cookie(response, new_token)
    session.refresh(user)
    # The first-run password file (app/initial_admin_password.py) goes with
    # the password it holds.
    remove_initial_admin_password(user)
    return UserPublic.from_user(user, session)


def _end_other_sessions(
    session: Session, user: User, session_token: str | None, *, reason: str
) -> None:
    """Ends every session of the user but the caller's, audited as
    session.revoke with `reason`. Doesn't commit."""
    revoked = revoke_all_sessions(session, user.id, except_token=session_token)
    if revoked:
        record_audit_event(
            session,
            user_id=user.id,
            action="session.revoke",
            target_type="user",
            target_id=user.id,
            metadata={"count": revoked, "reason": reason},
        )


EMAIL_NOT_USABLE = "That email address can't be used. Enter another one."

# An error code, not a password (Bandit B105).
CURRENT_PASSWORD_REQUIRED = "current_password_required"  # nosec B105


@router.patch("/me", response_model=UserPublic)
def update_my_profile(
    payload: ProfileUpdate,
    background: BackgroundTasks,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    """My profile (#243): the user's own display name and email. An SSO
    account's come from the IdP on every sign-in, so they can't be edited
    here.

    Changing the email takes the current password, throttled like the MFA
    calls: otherwise a stolen session could point the account at another
    mailbox and take it over through password recovery (#244). The old
    address is told, when email is set up and on."""
    changes = payload.model_dump(exclude_unset=True)
    current_password = changes.pop("current_password", None)
    if not changes:
        return UserPublic.from_user(user, session)
    if user.auth_provider != AuthProviderType.local:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your name and email come from your identity provider",
        )
    email_changing = False
    if "email" in changes:
        try:
            email_changing = profile.normalize_email(changes["email"]) != user.email
        except profile.ProfileError as exc:
            raise profile_error(exc) from exc
    old_email = user.email
    if email_changing:
        if not current_password:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Enter your current password to change your email",
                    "error_code": CURRENT_PASSWORD_REQUIRED,
                },
            )
        _require_password_reconfirmation(session, user, current_password, action="email_change")
    try:
        profile.apply_profile(session, user, actor_id=user.id, changes=changes)
    except profile.EmailTakenError:
        # Not a 409 naming the conflict: whoever holds a session shouldn't
        # learn which addresses other accounts have.
        logger.info("user.email_change_refused", user_id=user.id, reason="taken")
        raise HTTPException(status_code=422, detail=EMAIL_NOT_USABLE) from None
    except profile.ProfileError as exc:
        raise profile_error(exc) from exc
    session.commit()
    session.refresh(user)
    if email_changing and old_email:
        background.add_task(
            password_reset.notify_email_changed, session.get_bind(), user.id, old_email
        )
    return UserPublic.from_user(user, session)


@router.patch("/preferences", response_model=UserPublic)
def update_my_preferences(
    payload: PreferencesUpdate,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    """The signed-in user's own display preferences (#273). Not audited:
    they change nothing but how this user's screens look."""
    try:
        preferences.update(user, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.add(user)
    session.commit()
    session.refresh(user)
    return UserPublic.from_user(user, session)


@router.get("/password-policy", response_model=PasswordPolicyPublic)
def get_password_policy(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """The rules a new password must follow, shown next to password fields.
    Open to any signed-in user, including one who must change their
    password first."""
    return PasswordPolicyPublic(**password_policy.get_policy(session).as_dict())


class MfaEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaConfirmRequest(BaseModel):
    code: str


class MfaBackupCodesResponse(BaseModel):
    backup_codes: list[str]


class MfaPasswordConfirmRequest(BaseModel):
    # Re-proves the caller is actually the account owner, not just holding
    # an already-open session -- same reasoning as requiring the current
    # password on /auth/change-password. Without this, a session token
    # alone (stolen via XSS, a shared browser, etc.) would be enough to
    # silently enroll a new device, turn MFA off, or invalidate someone's
    # existing backup codes -- all of which are meant to require proving
    # you're still the account owner, not just holding a live cookie.
    password: str


class MfaDisableRequest(MfaPasswordConfirmRequest):
    # Turning MFA off also needs the second factor it protects: a password
    # caught by phishing plus a stolen session mustn't be enough. A TOTP
    # code or a backup code; required while MFA is on.
    code: str | None = None


def _require_password_reconfirmation(
    session: Session,
    user: User,
    password: str,
    *,
    action: str,
    mfa_code: str | None = None,
) -> None:
    """Shared by every /mfa/* endpoint that re-proves account ownership via
    password -- throttled exactly like /auth/login itself (keyed by
    username, via app.login_throttle), since without a lockout here a
    stolen session token would let an attacker brute-force the account
    password at argon2-timing speed with no cap on attempts.

    With `mfa_code`, the account's second factor is checked too (a TOTP
    code or a backup code, app/auth/mfa.py), on the same budget: the
    failures are cleared only once both the password and the code pass, so
    a known password can't be used to guess codes without limit.

    A wrong password or code is audited as user.reauth_failed, naming what
    it was meant to allow (`action`) and nothing about what was sent."""
    require_password_length(session, password)
    attempt = _ReconfirmationAttempt(session, user, action=action)
    if not verify_password(user, password):
        attempt.refuse("Current password is incorrect")
    # Whatever the check records (user.mfa_secret_unreadable, a spent backup
    # code) is committed with the refusal or with the caller's change.
    if mfa_code is not None and not mfa.verify_mfa_code(session, user, mfa_code):
        attempt.refuse("Invalid authentication code")
    attempt.succeeded()


class _ReconfirmationAttempt:
    """One attempt at re-proving account ownership, on the same budget as
    /auth/login (app/login_throttle.py): refused with 429 while locked out;
    each failure counts, is audited as user.reauth_failed naming `action`,
    and audits any lockout it triggers; success clears the failures."""

    def __init__(self, session: Session, user: User, *, action: str):
        self.session = session
        self.user = user
        self.action = action
        context = request_context.current()
        self.client_ip = context.client_ip if context is not None else None
        self.per_address = mfa_policy.emergency_account(user)
        remaining = seconds_until_allowed(
            user.username, self.client_ip, per_address_account=self.per_address
        )
        if remaining is not None:
            raise _too_many_attempts(remaining, "Too many failed attempts. Try again later.")

    def refuse(self, detail: str, *, status_code: int = status.HTTP_401_UNAUTHORIZED) -> NoReturn:
        user, session = self.user, self.session
        outcome = record_failure(
            user.username, self.client_ip, per_address_account=self.per_address
        )
        record_audit_event(
            session,
            user_id=user.id,
            action="user.reauth_failed",
            target_type="user",
            target_id=user.id,
            metadata={"action": self.action},
        )
        _audit_lockouts(
            session, user.username, self.client_ip, outcome, per_address_account=self.per_address
        )
        session.commit()
        raise HTTPException(status_code=status_code, detail=detail)

    def succeeded(self) -> None:
        record_success(self.user.username, self.client_ip, per_address_account=self.per_address)


@router.post("/mfa/enroll", response_model=MfaEnrollResponse)
def mfa_enroll(
    payload: MfaPasswordConfirmRequest,
    # Open to an account that must set MFA up before anything else.
    user: User = Depends(get_current_user_setting_up_mfa),
    session: Session = Depends(get_session),
):
    """Self-service only, same scope as change-password -- generates a new
    pending TOTP secret for an authenticator app to scan, but doesn't
    enable MFA yet (see /mfa/confirm). Requires re-confirming the current
    password (see MfaPasswordConfirmRequest) and refuses to start while MFA
    is already enabled -- re-enrolling over an active secret would silently
    replace it before a replacement is confirmed to actually work, bricking
    the account's current second factor. Disable first to re-enroll."""
    _require_password_reconfirmation(session, user, payload.password, action="mfa_enroll")
    if user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is already enabled — disable it first to re-enroll",
        )
    secret = mfa.start_enrollment(session, user)
    return MfaEnrollResponse(secret=secret, otpauth_uri=mfa.provisioning_uri(secret, user.username))


@router.post("/mfa/confirm", response_model=MfaBackupCodesResponse)
def mfa_confirm(
    payload: MfaConfirmRequest,
    user: User = Depends(get_current_user_setting_up_mfa),
    session: Session = Depends(get_session),
    session_token: str | None = Depends(session_cookie),
):
    """Confirms a pending enrollment (see /mfa/enroll) with a code from the
    new secret. Only a pending one: with MFA already on it's refused, and
    new backup codes come only from /mfa/backup-codes/regenerate, which
    needs the password. A wrong code counts toward the same lockout as a
    wrong password (_ReconfirmationAttempt), so this is no unlimited code
    oracle. Turning MFA on ends the account's other sessions."""
    if user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is already enabled"
        )
    attempt = _ReconfirmationAttempt(session, user, action="mfa_confirm")
    try:
        codes = mfa.confirm_enrollment(session, user, payload.code)
    except ValueError:
        attempt.refuse("Invalid authentication code", status_code=status.HTTP_400_BAD_REQUEST)
    attempt.succeeded()

    record_audit_event(
        session,
        user_id=user.id,
        action="user.mfa_enable",
        target_type="user",
        target_id=user.id,
        # Whether the account had to set it up (app/auth/mfa_policy.py).
        metadata={"required": mfa_policy.mfa_required(session, user)},
    )
    # A session opened without the second factor doesn't outlive it.
    _end_other_sessions(session, user, session_token, reason="mfa_enable")
    session.commit()
    return MfaBackupCodesResponse(backup_codes=codes)


@router.post("/mfa/disable", response_model=UserPublic)
def mfa_disable(
    payload: MfaDisableRequest,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
    session_token: str | None = Depends(session_cookie),
):
    """Turns the caller's own MFA off. Needs the current password and, while
    MFA is on, a TOTP or backup code (MfaDisableRequest). With MFA only
    pending (enrolled, not confirmed), there's no second factor to prove and
    the password is enough. Turning it off ends the account's other
    sessions."""
    was_enabled = user.mfa_enabled
    _require_password_reconfirmation(
        session,
        user,
        payload.password,
        action="mfa_disable",
        mfa_code=(payload.code or "") if user.mfa_enabled else None,
    )

    mfa.disable_mfa(session, user)
    record_audit_event(
        session, user_id=user.id, action="user.mfa_disable", target_type="user", target_id=user.id
    )
    if was_enabled:
        _end_other_sessions(session, user, session_token, reason="mfa_disable")
    session.commit()
    session.refresh(user)
    return UserPublic.from_user(user, session)


@router.post("/mfa/backup-codes/regenerate", response_model=MfaBackupCodesResponse)
def mfa_regenerate_backup_codes(
    payload: MfaPasswordConfirmRequest,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    _require_password_reconfirmation(
        session, user, payload.password, action="mfa_regenerate_backup_codes"
    )

    try:
        codes = mfa.regenerate_backup_codes(session, user)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is not enabled"
        ) from None

    record_audit_event(
        session,
        user_id=user.id,
        action="user.mfa_backup_codes_regenerated",
        target_type="user",
        target_id=user.id,
    )
    session.commit()
    return MfaBackupCodesResponse(backup_codes=codes)


class AuthSessionPublic(BaseModel):
    id: int
    created_at: str
    last_seen_at: str | None
    expires_at: str
    user_agent: str | None
    is_current: bool

    @classmethod
    def from_session(
        cls, auth_session: AuthSession, *, current_token_hash: str | None
    ) -> "AuthSessionPublic":
        return cls(
            id=auth_session.id,
            created_at=auth_session.created_at.isoformat(),
            last_seen_at=(
                auth_session.last_seen_at.isoformat() if auth_session.last_seen_at else None
            ),
            expires_at=auth_session.expires_at.isoformat(),
            user_agent=auth_session.user_agent,
            is_current=auth_session.token_hash == current_token_hash,
        )


@router.get("/sessions", response_model=list[AuthSessionPublic])
def list_sessions(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    session_token: str | None = Depends(session_cookie),
):
    """Self-service only -- a user's own active sessions, never another
    user's (see auth/sessions.py's list_sessions_for_user). Uses
    get_current_user rather than get_current_active_user so an
    admin-created account mid forced-password-change can still see (and if
    needed, revoke) its own sessions."""
    current_hash = hash_token(session_token) if session_token else None
    auth_sessions = list_sessions_for_user(session, user.id)
    return [
        AuthSessionPublic.from_session(s, current_token_hash=current_hash) for s in auth_sessions
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session_endpoint(
    session_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Revoking the session you're currently using is allowed (same as
    clicking "log out" on this device from another device's session list)
    -- the next request on that browser simply finds no matching
    AuthSession and gets redirected to /login by the existing auth guard,
    no special-casing needed here."""
    target = session.get(AuthSession, session_id)
    revoked_ref = request_context.session_ref(target.token_hash) if target is not None else None
    found = revoke_session(session, user_id=user.id, session_id=session_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    record_audit_event(
        session,
        user_id=user.id,
        action="session.revoke",
        target_type="user",
        target_id=user.id,
        metadata={"count": 1, "revoked_session_id": revoked_ref, "reason": "user_revoked"},
    )
    session.commit()


class SSOStatus(BaseModel):
    enabled: bool
    name: str | None = None


def _get_enabled_provider(session: Session) -> SSOProviderConfig | None:
    query = select(SSOProviderConfig).where(SSOProviderConfig.enabled.is_(True))
    return session.exec(query).first()


@router.get("/sso/status", response_model=SSOStatus)
def sso_status(session: Session = Depends(get_session)):
    """Public (no auth) — lets the login page show/hide a "Sign in with
    SSO" button without needing a round-trip through the admin-gated
    /sso/providers endpoints."""
    provider = _get_enabled_provider(session)
    if provider is None:
        return SSOStatus(enabled=False)
    return SSOStatus(enabled=True, name=provider.name)


@router.get("/sso/login")
def sso_login(request: Request, session: Session = Depends(get_session)):
    provider = _get_enabled_provider(session)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSO is not enabled")
    # Anyone can call this, and it may wait on the identity provider: capped
    # per address (app/login_throttle.py), on top of the discovery cache.
    client_ip = _client_ip(request)
    retry_after = sso_login_from_address(client_ip)
    if retry_after is not None:
        auth_events.sso_login_failed(session, client_ip=client_ip, reason="rate_limited")
        raise _too_many_attempts(retry_after, "Too many sign-in attempts. Try again later.")

    app_settings = get_settings()
    # Every failure past this point sends the browser back to the login page
    # with the same neutral SSO error as a failed callback (sso_callback),
    # rather than leaving it on a JSON body; the log says why.
    login_error_redirect = RedirectResponse(
        f"{app_settings.public_base_url}/#/login?sso_error=1",
        status_code=status.HTTP_302_FOUND,
    )
    raw_config = read_provider_config(provider.config)
    if raw_config is None:
        # The provider's settings can't be decrypted with the current key:
        # audited, and the browser gets the same neutral SSO error as any
        # other failed sign-in.
        logger.error("sso.login.config_unreadable", provider_id=provider.id)
        auth_events.sso_login_failed(
            session,
            client_ip=request.client.host if request.client else None,
            reason="secret_unreadable",
        )
        return login_error_redirect
    oidc_settings = parse_oidc_settings(raw_config)
    nonce = secrets.token_urlsafe(32)
    # PKCE (RFC 7636, S256): the verifier travels only inside the encrypted
    # state, the IdP sees its hash, and the code exchange must present it.
    code_verifier = generate_code_verifier()
    state = encode_state(nonce, code_verifier)
    redirect_uri = f"{app_settings.public_base_url}/auth/sso/callback"

    try:
        authorize_url = OIDCProvider(oidc_settings).authorization_redirect_url(
            redirect_uri=redirect_uri, state=state, nonce=nonce, code_verifier=code_verifier
        )
    except InsecureIdPURLError as exc:
        # Configured before HTTPS was required: a clear line for the admin
        # in the log, the usual answer for whoever tried to sign in.
        logger.error("sso.login.insecure_idp_url", error=str(exc))
        return login_error_redirect
    except IssuerMismatchError:
        # The discovery document speaks for another issuer: its endpoints
        # and keys aren't the configured IdP's. Audited, and answered like
        # any other unreachable IdP.
        logger.error("sso.login.issuer_mismatch")
        auth_events.sso_login_failed(
            session,
            client_ip=request.client.host if request.client else None,
            reason="issuer_mismatch",
        )
        return login_error_redirect
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean error, not a raw 500
        # Discovery failed: the IdP can't be reached, its address is refused
        # (_idp_request's address policy), or it answered with an error.
        logger.error("sso.login.discovery_failed", error=str(exc))
        return login_error_redirect

    response = RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)
    # Binds this flow to this browser (see SSO_STATE_COOKIE_NAME above) --
    # sso_callback rejects a state it can't match against this same cookie,
    # closing the "attacker replays their own valid code+state to a victim"
    # login-CSRF gap (issue #61). Same TTL as the state token itself; no
    # point outliving what decode_state would accept anyway.
    response.set_cookie(
        key=sso_state_cookie_name(),
        value=state,
        httponly=True,
        secure=app_settings.session_cookie_secure,
        samesite="lax",
        max_age=STATE_TTL_SECONDS,
        path="/",
    )
    return response


SSO_ERROR_LOG_MAX = 200


def _log_cut(value: str | None) -> str | None:
    """At most SSO_ERROR_LOG_MAX characters of a value the caller sent."""
    return value[:SSO_ERROR_LOG_MAX] if value is not None else None


@router.get("/sso/callback")
def sso_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    sso_state_cookie: str | None = Depends(read_sso_state_cookie),
    session_token: str | None = Depends(session_cookie),
    session: Session = Depends(get_session),
):
    app_settings = get_settings()
    login_error_redirect = RedirectResponse(
        f"{app_settings.public_base_url}/#/login?sso_error=1", status_code=status.HTTP_302_FOUND
    )
    # One-time cookie either way -- a failed attempt shouldn't leave it
    # sitting around for a retry to reuse, and a successful one is about to
    # get its own real session cookie instead.
    _clear_sso_state_cookie(login_error_redirect)
    client_ip = request.client.host if request.client else None

    def failed(reason: str, error_detail: str | None = None) -> RedirectResponse:
        # Every way the callback can fail is audited (folded like failed
        # logins, since it needs no session); the browser only learns that
        # it failed.
        auth_events.sso_login_failed(
            session, client_ip=client_ip, reason=reason, error=error_detail
        )
        return login_error_redirect

    if error is not None or code is None or state is None:
        # Sent by whoever followed the link, not only by the IdP: cut short
        # before it reaches the log.
        logger.warning(
            "sso.callback.idp_error",
            error=_log_cut(error),
            error_description=_log_cut(error_description),
        )
        return failed("idp_error", error)

    # Login-CSRF / state-fixation guard (issue #61): `state` must match the
    # cookie /sso/login set in THIS browser. Without this, an attacker who
    # holds any valid IdP account can complete their own login, capture the
    # resulting code+state before their own browser follows it, and hand
    # that URL to a victim -- whose browser would otherwise happily
    # exchange it and receive a session for the attacker's account. A
    # forged URL carries a `state` value but never the matching cookie,
    # since setting that requires the victim's own browser to have visited
    # /sso/login itself.
    if sso_state_cookie is None or not secrets.compare_digest(sso_state_cookie, state):
        logger.warning("sso.callback.state_cookie_mismatch")
        return failed("state_cookie_mismatch")

    try:
        nonce, code_verifier = decode_state(state)
    except Exception:  # noqa: BLE001 - any decode failure means "start the login over"
        logger.warning("sso.callback.invalid_state")
        return failed("invalid_state")

    provider = _get_enabled_provider(session)
    if provider is None:
        return failed("sso_disabled")

    raw_config = read_provider_config(provider.config)
    if raw_config is None:
        logger.error("sso.callback.config_unreadable", provider_id=provider.id)
        return failed("secret_unreadable")
    oidc_settings = parse_oidc_settings(raw_config)
    no_access_role = session.exec(select(Role).where(Role.name == NO_ACCESS_ROLE_NAME)).first()
    if no_access_role is None:
        # Seeded at startup (app.bootstrap.seed_no_access_role) — missing only
        # if the app never finished starting up, which shouldn't reach here.
        logger.error("sso.callback.no_access_role_missing")
        return failed("no_access_role_missing")

    redirect_uri = f"{app_settings.public_base_url}/auth/sso/callback"
    try:
        user = OIDCProvider(oidc_settings).complete_login(
            session,
            code=code,
            redirect_uri=redirect_uri,
            nonce=nonce,
            code_verifier=code_verifier,
            no_access_role_id=no_access_role.id,
        )
    except IssuerMismatchError:
        # See sso_login. Raised by the discovery, before any request that
        # carries the code or the client secret.
        logger.error("sso.callback.issuer_mismatch")
        session.rollback()
        return failed("issuer_mismatch")
    except Exception as exc:  # noqa: BLE001 - any failure here means "login didn't succeed"
        logger.warning("sso.callback.login_failed", error=str(exc))
        # Rolled back first: whatever the provider got half way through
        # (a provisioned account, a role change) must not be committed with
        # the audit entry. Only the kind of error is recorded, not its text,
        # which can quote claims from the token.
        session.rollback()
        return failed("login_failed", type(exc).__name__)

    # As at a local sign-in: the browser's previous session ends here.
    if session_token:
        end_previous_session(session, session_token, signed_in=user)
    _, token = create_session(session, user, user_agent=request.headers.get("user-agent"))
    response = RedirectResponse(
        f"{app_settings.public_base_url}/", status_code=status.HTTP_302_FOUND
    )
    _clear_sso_state_cookie(response)
    _set_session_cookie(response, token)
    return response


# --- Password recovery by email (#244, app/auth/password_reset.py) -----------

_NO_STORE = {"Cache-Control": "no-store"}


class _NoStoreRoute(APIRoute):
    """Every answer of these routes, errors included, carries
    Cache-Control: no-store: they handle reset tokens and passwords."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), **_NO_STORE}
                raise
            except RequestValidationError as exc:
                # Handled here, not by the app's handler: logged the same way.
                from app.api.errors import log_validation_failure, validation_response

                log_validation_failure(request, exc)
                response = validation_response(exc)
            response.headers.update(_NO_STORE)
            return response

        return no_store_handler


_reset_router = APIRouter(route_class=_NoStoreRoute)

INVALID_RESET_LINK = "This link is invalid or has expired. Ask for a new one."


class LoginOptions(BaseModel):
    # Whether the login page offers "Forgot password?": recovery is turned
    # on and email is ready for it.
    password_reset: bool


class PasswordResetRequest(BaseModel):
    # A username or an email address.
    identifier: str = Field(max_length=1024)


class PasswordResetAccepted(BaseModel):
    # The same whatever the identifier matched.
    status: str = "accepted"


class PasswordResetTokenBody(BaseModel):
    token: str = Field(max_length=256)


class PasswordResetCheck(BaseModel):
    valid: bool
    # The rules the new password must follow; only with a valid token.
    policy: PasswordPolicyPublic | None = None


class PasswordResetComplete(PasswordResetTokenBody):
    new_password: str = Field(max_length=4096)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _require_password_reset(session: Session) -> None:
    """Off, or not usable (no email, no https): the routes don't exist."""
    if not password_reset.is_active(session):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _limit_token_attempts(session: Session, client_ip: str | None) -> None:
    retry_after = password_reset_limits.token_attempt_from_address(client_ip)
    if retry_after is not None:
        auth_events.password_reset_failed(session, client_ip=client_ip, reason="rate_limited")
        raise _too_many_attempts(retry_after, "Too many attempts. Try again later.")


def _invalid_reset_link() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"message": INVALID_RESET_LINK, "error_code": "reset_link_invalid"},
    )


@_reset_router.get("/login-options", response_model=LoginOptions)
def login_options(session: Session = Depends(get_session)):
    """Public (no auth): what the login page offers besides signing in."""
    return LoginOptions(password_reset=password_reset.is_active(session))


@_reset_router.post(
    "/password-reset/request",
    response_model=PasswordResetAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Public: asks for a reset link. Always the same answer, at once,
    whether or not an account matched; finding it, the per-account limit,
    the link and the email happen after the response
    (password_reset.request_reset), so neither the answer nor its timing
    says whether the account exists."""
    _require_password_reset(session)
    identifier = password_reset.valid_identifier(payload.identifier)
    if identifier is None:
        raise HTTPException(status_code=422, detail="Enter your username or email address")
    client_ip = _client_ip(request)
    retry_after = password_reset_limits.request_from_address(client_ip)
    if retry_after is not None:
        auth_events.password_reset_requested(
            session, client_ip=client_ip, result="rate_limited", identifier=identifier
        )
        raise _too_many_attempts(retry_after, "Too many requests. Try again later.")
    background.add_task(password_reset.request_reset, session.get_bind(), identifier, client_ip)
    return PasswordResetAccepted()


@_reset_router.post("/password-reset/check", response_model=PasswordResetCheck)
def check_password_reset(
    payload: PasswordResetTokenBody, request: Request, session: Session = Depends(get_session)
):
    """Public: whether a reset link can still be used, and the password
    rules when it can. A link that can't be used is audited like a refused
    completion."""
    _require_password_reset(session)
    client_ip = _client_ip(request)
    _limit_token_attempts(session, client_ip)
    result = password_reset.check_token(session, payload.token)
    session.commit()
    if result.failure is not None:
        auth_events.password_reset_failed(
            session, client_ip=client_ip, reason=result.failure, user=result.user
        )
        return PasswordResetCheck(valid=False)
    return PasswordResetCheck(
        valid=True, policy=PasswordPolicyPublic(**password_policy.get_policy(session).as_dict())
    )


@_reset_router.post("/password-reset/complete", status_code=status.HTTP_204_NO_CONTENT)
def complete_password_reset(
    payload: PasswordResetComplete, request: Request, session: Session = Depends(get_session)
):
    """Public: sets a new password through a reset link. Ends every session
    of the account and opens none: the user signs in again, second factor
    included. Every refusal of the link gets the same answer."""
    _require_password_reset(session)
    require_password_length(session, payload.new_password)
    client_ip = _client_ip(request)
    _limit_token_attempts(session, client_ip)
    result = password_reset.check_token(session, payload.token)
    if result.failure is not None or result.user is None or result.row is None:
        session.commit()
        auth_events.password_reset_failed(
            session, client_ip=client_ip, reason=result.failure or "invalid_token", user=result.user
        )
        raise _invalid_reset_link()
    user = result.user
    try:
        password_reset.complete_reset(session, user, result.row, payload.new_password)
    except password_policy.PasswordPolicyError as exc:
        session.rollback()
        auth_events.password_reset_failed(session, client_ip=client_ip, reason="policy", user=user)
        raise password_policy_error(exc) from exc
    except password_reset.TokenAlreadyUsedError:
        session.rollback()
        auth_events.password_reset_failed(
            session, client_ip=client_ip, reason="invalid_token", user=user
        )
        raise _invalid_reset_link() from None


router.include_router(_reset_router)
