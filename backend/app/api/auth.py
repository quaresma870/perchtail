from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from app.auth.models import User
from app.auth.providers.local import LocalPasswordProvider, change_password, verify_password
from app.auth.sessions import create_session, delete_session, get_user_by_token
from app.config import get_settings
from app.db import get_session

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
    return user


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
    return user


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
    return user
