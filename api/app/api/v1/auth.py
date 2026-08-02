from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.errors import AuthenticationError, ValidationError
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.identity import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    UserOut,
)
from app.schemas.common import Message
from app.services import audit, auth

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"


def _set_refresh_cookie(response: Response, token: str) -> None:
    """httpOnly so JavaScript can never read it — this is the XSS defence.

    The access token lives in memory on the client and is never persisted.
    """
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=settings.refresh_token_days * 24 * 3600,
        path="/api/v1/auth",
    )


@router.post("/token", include_in_schema=False)
def token(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Token endpoint for the Swagger "Authorize" button only.

    OAuth2's password flow insists the field be called `username`; ours is an
    email address, so it is simply passed through. The React client uses
    /login instead — this exists so the interactive docs are usable without
    copying a JWT around by hand.
    """
    access, _refresh, _user = auth.authenticate(
        db,
        form.username,
        form.password,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    return {"access_token": access, "token_type": "bearer"}


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    access, refresh, user = auth.authenticate(
        db,
        payload.email,
        payload.password,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, refresh)
    audit.record(
        db,
        action="auth.login",
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        ip=request.client.host if request.client else None,
    )
    return LoginResponse(
        access_token=access,
        expires_in=settings.access_token_minutes * 60,
        user=UserOut.from_user(user),
    )


@router.post("/refresh", response_model=LoginResponse)
def refresh(
    request: Request, response: Response, db: Session = Depends(get_db)
) -> LoginResponse:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AuthenticationError("No refresh token present")

    access, new_refresh, user = auth.rotate_refresh_token(
        db,
        token,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, new_refresh)
    return LoginResponse(
        access_token=access,
        expires_in=settings.access_token_minutes * 60,
        user=UserOut.from_user(user),
    )


@router.post("/logout", response_model=Message)
def logout(
    request: Request, response: Response, db: Session = Depends(get_db)
) -> Message:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        auth.revoke_refresh_token(db, token)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    return Message(message="Signed out")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.from_user(user)


@router.post("/change-password", response_model=Message)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Message:
    if not verify_password(payload.current_password, user.password_hash):
        raise AuthenticationError("Current password is incorrect")
    if payload.current_password == payload.new_password:
        raise ValidationError("New password must differ from the current one")

    user.password_hash = hash_password(payload.new_password)
    audit.record(
        db,
        action="auth.change_password",
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        ip=request.client.host if request.client else None,
    )
    return Message(message="Password updated")
