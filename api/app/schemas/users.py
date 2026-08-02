from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None = None
    #: What the role can actually do. Shown when assigning one, so the choice
    #: is made on capabilities rather than on a job title.
    permissions: list[str] = []


class UserListOut(BaseModel):
    """Deliberately without `permissions` — a list of 40 users would otherwise
    carry 40 copies of the same role bundle. Roles are fetched once instead."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role_id: int
    role_code: str
    role_name: str
    warehouse_id: int | None = None
    warehouse_name: str | None = None
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime


class UserCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "email": "kavita@pharmacy.co.in",
        "full_name": "Kavita Menon",
        "role_id": 3,
        "warehouse_id": 2,
        "password": "ChangeMe@2026",
    }})

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    role_id: int
    #: Required for branch staff, forbidden for admins and managers — see the
    #: rule in the endpoint. Null means "the whole chain".
    warehouse_id: int | None = None
    #: bcrypt truncates past 72 bytes, so the ceiling is the algorithm's.
    password: str = Field(min_length=8, max_length=72)


class UserUpdate(BaseModel):
    """Every field optional; only what is sent gets changed.

    `email` is absent on purpose. It is the login identity and appears in the
    audit trail, so changing it would silently rewrite who past entries point
    at. Retire the account and create a new one instead.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role_id: int | None = None
    warehouse_id: int | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "new_password": "TempPass@2026"
    }})

    new_password: str = Field(min_length=8, max_length=72)
