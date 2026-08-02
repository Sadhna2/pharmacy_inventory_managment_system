"""Authentication: login, refresh-token rotation, logout."""

import secrets
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.models.identity import RefreshToken, Role, RolePermission, User


def _load_user(db: Session, **where) -> User | None:
    return db.scalar(
        select(User)
        .options(
            selectinload(User.role)
            .selectinload(Role.permissions)
            .selectinload(RolePermission.permission)
        )
        .filter_by(**where)
    )


def authenticate(
    db: Session, email: str, password: str, *, user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, str, User]:
    """Return (access_token, refresh_token_plaintext, user)."""
    user = _load_user(db, email=email.lower().strip())

    # Verify against a dummy hash when the user is missing, so a non-existent
    # account takes the same time as a wrong password.
    if user is None:
        verify_password(password, "$2b$12$" + "x" * 53)
        raise AuthenticationError("Incorrect email or password")

    if not verify_password(password, user.password_hash):
        raise AuthenticationError("Incorrect email or password")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated")

    access, _ = create_access_token(
        user_id=user.id, role=user.role.code, permissions=user.permission_codes
    )
    raw, token_hash, expires = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            family_id=secrets.token_urlsafe(32),
            expires_at=expires,
            user_agent=user_agent,
            ip=ip,
        )
    )
    user.last_login_at = datetime.now(UTC)
    db.flush()
    return access, raw, user


def rotate_refresh_token(
    db: Session, raw_token: str, *, user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, str, User]:
    """Exchange a refresh token for a new pair.

    Presenting an already-consumed token means it leaked, so the entire
    rotation family is revoked and the session is killed.
    """
    token_hash = hash_refresh_token(raw_token)
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    if stored is None:
        raise AuthenticationError("Invalid refresh token")

    if stored.consumed_at is not None:
        # Token reuse detected — assume theft, revoke the whole chain.
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == stored.family_id)
            .values(revoked_at=datetime.now(UTC))
        )
        db.flush()
        raise AuthenticationError(
            "Refresh token was already used. All sessions have been revoked; "
            "please sign in again."
        )

    if stored.revoked_at is not None:
        raise AuthenticationError("Refresh token has been revoked")
    if stored.expires_at < datetime.now(UTC):
        raise AuthenticationError("Refresh token has expired")

    user = _load_user(db, id=stored.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is no longer active")

    stored.consumed_at = datetime.now(UTC)

    access, _ = create_access_token(
        user_id=user.id, role=user.role.code, permissions=user.permission_codes
    )
    raw, new_hash, expires = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=new_hash,
            family_id=stored.family_id,  # same chain
            expires_at=expires,
            user_agent=user_agent,
            ip=ip,
        )
    )
    db.flush()
    return access, raw, user


def revoke_refresh_token(db: Session, raw_token: str) -> None:
    stored = db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(raw_token)
        )
    )
    if stored is not None:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == stored.family_id)
            .values(revoked_at=datetime.now(UTC))
        )
        db.flush()
