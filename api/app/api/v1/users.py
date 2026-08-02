"""User administration.

Everything here is gated on `user.manage`, which only ADMIN holds. That is the
point: the permission has existed since Layer 0 and guarded nothing, so the
only way to add a pharmacist was to edit the seed script.

Passwords are write-only. There is no endpoint that returns a hash, and the
list response has no password field at all — an admin can set one, never read
one back.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.permissions import ADMIN, MANAGER, STAFF
from app.core.security import hash_password
from app.db.session import get_db
from app.models.identity import RefreshToken, Role, User
from app.models.masters import Warehouse
from app.schemas.common import Message, Page, PageParams, paginate
from app.schemas.users import (
    PasswordReset,
    RoleOut,
    UserCreate,
    UserListOut,
    UserUpdate,
)
from app.services import audit

router = APIRouter(prefix="/users", tags=["users"])
roles_router = APIRouter(prefix="/roles", tags=["users"])

MANAGE = require_permission("user.manage")


def _validate_scope(db: Session, role: Role, warehouse_id: int | None) -> None:
    """Branch staff belong to exactly one branch; managers and admins to none.

    This is not decoration. `scoped_warehouse_ids()` reads `warehouse_id` to
    decide what a user may see, and a STAFF row with no branch resolves to an
    empty list — an account that can log in and see nothing at all. Catching it
    here beats debugging it later.
    """
    if role.code == STAFF:
        if warehouse_id is None:
            raise ValidationError(
                "Branch staff must be assigned to a location — "
                "their stock visibility is scoped to it."
            )
        if db.get(Warehouse, warehouse_id) is None:
            raise NotFoundError(f"Warehouse {warehouse_id} not found")
    elif role.code in (ADMIN, MANAGER) and warehouse_id is not None:
        raise ValidationError(
            f"{role.name}s oversee the whole chain, so they are not "
            f"assigned to a single location."
        )


def _row(user: User) -> UserListOut:
    return UserListOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role_id=user.role_id,
        role_code=user.role.code,
        role_name=user.role.name,
        warehouse_id=user.warehouse_id,
        warehouse_name=user.warehouse.name if user.warehouse else None,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@roles_router.get("", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db), _: User = Depends(MANAGE)):
    """Roles with the permissions each one carries.

    Fetched once by the user form so the admin sees what a role actually grants
    before assigning it, rather than inferring it from the name.
    """
    roles = db.scalars(select(Role).order_by(Role.id)).all()
    return [
        RoleOut(
            id=r.id,
            code=r.code,
            name=r.name,
            description=r.description,
            permissions=sorted(rp.permission.code for rp in r.permissions),
        )
        for r in roles
    ]


@router.get("", response_model=Page[UserListOut])
def list_users(
    q: str | None = Query(None, description="Matches name or email"),
    role_id: int | None = None,
    warehouse_id: int | None = None,
    is_active: bool | None = None,
    page: int = 1,
    size: int = 25,
    db: Session = Depends(get_db),
    _: User = Depends(MANAGE),
) -> Page[UserListOut]:
    stmt = select(User).join(Role, Role.id == User.role_id)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(User.full_name.ilike(pattern), User.email.ilike(pattern))
        )
    if role_id is not None:
        stmt = stmt.where(User.role_id == role_id)
    if warehouse_id is not None:
        stmt = stmt.where(User.warehouse_id == warehouse_id)
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    users = db.scalars(
        stmt.order_by(User.is_active.desc(), User.full_name)
        .offset(params.offset)
        .limit(params.size)
    ).all()
    return paginate([_row(u) for u in users], total, params)


@router.post("", response_model=UserListOut, status_code=201)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(MANAGE),
) -> UserListOut:
    email = payload.email.lower()
    if db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise ConflictError(f"{email} already has an account")

    role = db.get(Role, payload.role_id)
    if role is None:
        raise NotFoundError(f"Role {payload.role_id} not found")
    _validate_scope(db, role, payload.warehouse_id)

    user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role_id=role.id,
        warehouse_id=payload.warehouse_id,
    )
    db.add(user)
    db.flush()
    audit.record(
        db,
        action="user.create",
        entity_type="user",
        entity_id=user.id,
        actor_user_id=actor.id,
        # The password is not in here, and must never be.
        after={
            "email": user.email,
            "full_name": user.full_name,
            "role": role.code,
            "warehouse_id": user.warehouse_id,
        },
    )
    db.refresh(user)
    return _row(user)


@router.patch("/{user_id}", response_model=UserListOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(MANAGE),
) -> UserListOut:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")

    changes = payload.model_dump(exclude_unset=True)

    # An admin demoting or disabling their own account locks the last door on
    # the way out. Someone else with `user.manage` has to do it.
    if user.id == actor.id:
        if changes.get("is_active") is False:
            raise ValidationError("You cannot deactivate your own account")
        if "role_id" in changes and changes["role_id"] != user.role_id:
            raise ValidationError("You cannot change your own role")

    role = db.get(Role, changes["role_id"]) if "role_id" in changes else user.role
    if role is None:
        raise NotFoundError(f"Role {changes['role_id']} not found")

    # The scope rule spans two fields, so it has to be checked against the
    # values the row will end up with, not the ones that happen to be in the
    # payload — changing role alone can invalidate an existing warehouse.
    warehouse_id = changes.get("warehouse_id", user.warehouse_id)
    _validate_scope(db, role, warehouse_id)
    changes["warehouse_id"] = warehouse_id

    before = {field: getattr(user, field) for field in changes}
    for field, value in changes.items():
        setattr(user, field, value)
    db.flush()
    audit.record(
        db,
        action="user.update",
        entity_type="user",
        entity_id=user.id,
        actor_user_id=actor.id,
        before={k: str(v) for k, v in before.items()},
        after={k: str(v) for k, v in changes.items()},
    )
    db.refresh(user)
    return _row(user)


@router.post("/{user_id}/password", response_model=Message)
def reset_password(
    user_id: int,
    payload: PasswordReset,
    db: Session = Depends(get_db),
    actor: User = Depends(MANAGE),
) -> Message:
    """Admin reset, for the "I'm locked out" call. No current password needed.

    Every refresh token the account holds is revoked, so a session opened with
    the old password — including one an attacker already had — dies with it.
    Users changing their own password use /auth/password instead.
    """
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")

    user.password_hash = hash_password(payload.new_password)
    revoked = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    ).rowcount
    db.flush()
    audit.record(
        db,
        action="user.password_reset",
        entity_type="user",
        entity_id=user.id,
        actor_user_id=actor.id,
        after={"sessions_revoked": revoked},
    )
    return Message(
        message=f"Password reset for {user.email}. {revoked} active session(s) signed out."
    )
