import json
import math
import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.auth.models import AuthSession, GlobalCapability, Role, SSOProviderConfig, User
from app.auth.providers.local import LocalPasswordProvider, change_password, verify_password
from app.auth.providers.oidc import (
    STATE_TTL_SECONDS,
    OIDCProvider,
    decode_state,
    encode_state,
    parse_oidc_settings,
)
from app.auth.sessions import (
    create_session,
    delete_session,
    get_user_by_token,
    hash_token,
    list_sessions_for_user,
    revoke_session,
)
from app.bootstrap import NO_ACCESS_ROLE_NAME
from app.config import get_settings
from app.crypto import decrypt_secret
from app.db import get_session
from app.logging_config import get_logger
from app.login_throttle import record_failure, record_success, seconds_until_unlocked

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

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role_id: int
    active: bool
    must_change_password: bool
    # Denormalized from the user's role so the frontend can gate nav/UI
    # (e.g. show the Roles/Users admin pages) without a second round-trip or
    # needing manage_roles just to read its own role's shape.
    is_super_admin: bool
    global_capabilities: list[GlobalCapability]

    @classmethod
    def from_user(cls, user: User) -> "UserPublic":
        return cls(
            id=user.id,
            username=user.username,
            role_id=user.role_id,
            active=user.active,
            must_change_password=user.must_change_password,
            is_super_admin=user.role.is_super_admin,
            global_capabilities=user.role.global_capabilities,
        )


def get_current_user(
    session: Session = Depends(get_session),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> User:
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user = get_user_by_token(session, session_token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    return user


def get_current_active_user(user: User = Depends(get_current_user)) -> User:
    """Blocks every endpoint except login/me/change-password until an
    admin-created account's forced password change is done."""
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Password change required"
        )
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        max_age=settings.session_ttl_hours * 3600,
    )


def _clear_sso_state_cookie(response: Response) -> None:
    # Match the attributes it was set with (see sso_login) -- Response
    # .delete_cookie()'s own defaults (secure=False) wouldn't necessarily
    # clear a cookie that was set with secure=True.
    settings = get_settings()
    response.delete_cookie(
        SSO_STATE_COOKIE_NAME,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


@router.post("/login", response_model=UserPublic)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    remaining = seconds_until_unlocked(payload.username)
    if remaining is not None:
        retry_after = math.ceil(remaining)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    user = LocalPasswordProvider().authenticate(session, payload.username, payload.password)
    if user is None:
        record_failure(payload.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    record_success(payload.username)
    _, token = create_session(session, user, user_agent=request.headers.get("user-agent"))
    _set_session_cookie(response, token)
    return UserPublic.from_user(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    session: Session = Depends(get_session),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    if session_token is not None:
        delete_session(session, session_token)
    response.delete_cookie(SESSION_COOKIE_NAME)


@router.get("/me", response_model=UserPublic)
def me(user: User = Depends(get_current_user)):
    return UserPublic.from_user(user)


@router.post("/change-password", response_model=UserPublic)
def change_password_endpoint(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not verify_password(user, payload.current_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect"
        )

    change_password(session, user, payload.new_password)
    session.refresh(user)
    return UserPublic.from_user(user)


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
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
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
    found = revoke_session(session, user_id=user.id, session_id=session_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")


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
def sso_login(session: Session = Depends(get_session)):
    provider = _get_enabled_provider(session)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSO is not enabled")

    oidc_settings = parse_oidc_settings(json.loads(decrypt_secret(provider.config)))
    nonce = secrets.token_urlsafe(32)
    state = encode_state(nonce)
    app_settings = get_settings()
    redirect_uri = f"{app_settings.public_base_url}/auth/sso/callback"

    try:
        authorize_url = OIDCProvider(oidc_settings).authorization_redirect_url(
            redirect_uri=redirect_uri, state=state, nonce=nonce
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean error, not a raw 500
        logger.error("sso.login.discovery_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach the identity provider"
        ) from exc

    response = RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)
    # Binds this flow to this browser (see SSO_STATE_COOKIE_NAME above) --
    # sso_callback rejects a state it can't match against this same cookie,
    # closing the "attacker replays their own valid code+state to a victim"
    # login-CSRF gap (issue #61). Same TTL as the state token itself; no
    # point outliving what decode_state would accept anyway.
    response.set_cookie(
        key=SSO_STATE_COOKIE_NAME,
        value=state,
        httponly=True,
        secure=app_settings.session_cookie_secure,
        samesite="lax",
        max_age=STATE_TTL_SECONDS,
    )
    return response


@router.get("/sso/callback")
def sso_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    sso_state_cookie: str | None = Cookie(default=None, alias=SSO_STATE_COOKIE_NAME),
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

    if error is not None or code is None or state is None:
        logger.warning("sso.callback.idp_error", error=error)
        return login_error_redirect

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
        return login_error_redirect

    try:
        nonce = decode_state(state)
    except Exception:  # noqa: BLE001 - any decode failure means "start the login over"
        logger.warning("sso.callback.invalid_state")
        return login_error_redirect

    provider = _get_enabled_provider(session)
    if provider is None:
        return login_error_redirect

    oidc_settings = parse_oidc_settings(json.loads(decrypt_secret(provider.config)))
    no_access_role = session.exec(select(Role).where(Role.name == NO_ACCESS_ROLE_NAME)).first()
    if no_access_role is None:
        # Seeded at startup (app.bootstrap.seed_no_access_role) — missing only
        # if the app never finished starting up, which shouldn't reach here.
        logger.error("sso.callback.no_access_role_missing")
        return login_error_redirect

    redirect_uri = f"{app_settings.public_base_url}/auth/sso/callback"
    try:
        user = OIDCProvider(oidc_settings).complete_login(
            session,
            code=code,
            redirect_uri=redirect_uri,
            nonce=nonce,
            no_access_role_id=no_access_role.id,
        )
    except Exception as exc:  # noqa: BLE001 - any failure here means "login didn't succeed"
        logger.warning("sso.callback.login_failed", error=str(exc))
        return login_error_redirect

    _, token = create_session(session, user, user_agent=request.headers.get("user-agent"))
    response = RedirectResponse(
        f"{app_settings.public_base_url}/", status_code=status.HTTP_302_FOUND
    )
    _clear_sso_state_cookie(response)
    _set_session_cookie(response, token)
    return response
