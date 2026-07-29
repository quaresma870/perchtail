import json
import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.auth.models import GlobalCapability, Role, SSOProviderConfig, User
from app.auth.providers.local import LocalPasswordProvider, change_password, verify_password
from app.auth.providers.oidc import OIDCProvider, decode_state, encode_state, parse_oidc_settings
from app.auth.sessions import create_session, delete_session, get_user_by_token
from app.bootstrap import NO_ACCESS_ROLE_NAME
from app.config import get_settings
from app.crypto import decrypt_secret
from app.db import get_session
from app.logging_config import get_logger

logger = get_logger(__name__)

SESSION_COOKIE_NAME = "perchtail_session"

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


@router.post("/login", response_model=UserPublic)
def login(payload: LoginRequest, response: Response, session: Session = Depends(get_session)):
    user = LocalPasswordProvider().authenticate(session, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    _, token = create_session(session, user)
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

    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@router.get("/sso/callback")
def sso_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
):
    app_settings = get_settings()
    login_error_redirect = RedirectResponse(
        f"{app_settings.public_base_url}/#/login?sso_error=1", status_code=status.HTTP_302_FOUND
    )

    if error is not None or code is None or state is None:
        logger.warning("sso.callback.idp_error", error=error)
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

    _, token = create_session(session, user)
    response = RedirectResponse(
        f"{app_settings.public_base_url}/", status_code=status.HTTP_302_FOUND
    )
    _set_session_cookie(response, token)
    return response
