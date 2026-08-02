"""FastAPI dependencies: current user, permission gates, warehouse scoping."""

from collections.abc import Callable

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AuthenticationError, NotFoundError, PermissionDenied
from app.core.permissions import ADMIN, MANAGER
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.identity import Role, RolePermission, User

# OAuth2 password flow rather than a bare bearer scheme purely for the docs:
# it makes Swagger's Authorize dialog ask for an email and password and wire
# the returned token into every subsequent request. On the wire it is still an
# ordinary `Authorization: Bearer <jwt>` header, so the React client is
# unaffected. auto_error=False so a missing header raises our own 401 shape.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="api/v1/auth/token", auto_error=False
)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise AuthenticationError("Missing bearer token")

    payload = decode_access_token(token)
    user = db.scalar(
        select(User)
        .options(
            selectinload(User.role)
            .selectinload(Role.permissions)
            .selectinload(RolePermission.permission)
        )
        .where(User.id == int(payload["sub"]))
    )
    if user is None or not user.is_active:
        raise AuthenticationError("Account is no longer active")
    return user


def require_permission(*codes: str) -> Callable[..., User]:
    """Gate an endpoint on one or more permissions.

    Authorisation is enforced HERE, server-side, on every request. The frontend
    hiding a button is UX, not security.
    """

    def dependency(user: User = Depends(get_current_user)) -> User:
        granted = set(user.permission_codes)
        missing = [code for code in codes if code not in granted]
        if missing:
            raise PermissionDenied(
                f"This action requires: {', '.join(missing)}"
            )
        return user

    return dependency


def require_feature(key: str) -> Callable[..., None]:
    """Close an endpoint when an administrator has switched its feature off.

    Enforced server-side alongside the permission check, not merely used to
    hide a menu item. "Off" has to mean the API refuses, or turning a
    misbehaving feature off in front of an audience achieves nothing — anyone
    with the URL, or a stale tab, carries on calling it.

    404 rather than 403: to a client, a switched-off capability is
    indistinguishable from one this build does not have, and saying "forbidden"
    would imply the right permission would open it.
    """

    def dependency(db: Session = Depends(get_db)) -> None:
        from app.services import settings as settings_service

        if not settings_service.features(db).get(key, False):
            raise NotFoundError(
                "This feature is switched off. An administrator can turn it "
                "back on under Setup → Settings."
            )

    return dependency


def scoped_warehouse_ids(user: User) -> list[int] | None:
    """Which warehouses this user may see. None means all of them.

    Staff are pinned to their branch; managers and admins see the whole chain.
    """
    if user.role.code in (ADMIN, MANAGER):
        return None
    if user.warehouse_id is None:
        return []
    return [user.warehouse_id]


def get_request_context(request: Request) -> dict[str, str | None]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": getattr(request.state, "request_id", None),
    }
