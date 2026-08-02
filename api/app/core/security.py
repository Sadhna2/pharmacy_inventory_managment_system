"""Password hashing and JWT issue/verify.

Design notes (see ARCHITECTURE.md §10):
  - Access tokens are short-lived and carry role + permissions so the frontend
    can hide controls. Authorisation is still enforced server-side on every call.
  - Refresh tokens are opaque random strings; only their SHA-256 hash is stored,
    and they rotate on every use. Reuse of a consumed token revokes the family.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import AuthenticationError

# bcrypt truncates silently past 72 bytes; reject rather than let a long
# password be quietly equivalent to its prefix.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    pw = password.encode()
    if len(pw) > MAX_PASSWORD_BYTES:
        raise ValueError("password exceeds 72 bytes")
    return bcrypt.hashpw(pw, bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:MAX_PASSWORD_BYTES], hashed.encode())
    except (ValueError, TypeError):
        return False


def create_access_token(
    *, user_id: int, role: str, permissions: list[str]
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "permissions": permissions,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": secrets.token_urlsafe(16),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
    return token, expires


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Access token has expired") from None
    except jwt.InvalidTokenError:
        raise AuthenticationError("Invalid access token") from None

    if payload.get("typ") != "access":
        raise AuthenticationError("Wrong token type")
    return payload


def new_refresh_token() -> tuple[str, str, datetime]:
    """Return (plaintext, sha256_hash, expiry). Only the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    return (
        raw,
        hash_refresh_token(raw),
        datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
    )


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
