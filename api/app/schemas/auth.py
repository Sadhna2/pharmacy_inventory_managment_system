from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "email": "manager@pharmacy.co.in",
            "password": "the value of SEED_PASSWORD you seeded with"
    }})

    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: str
    permissions: list[str]
    warehouse_id: int | None = None
    warehouse_name: str | None = None
    is_active: bool
    last_login_at: datetime | None = None

    @classmethod
    def from_user(cls, user) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.code,
            permissions=user.permission_codes,
            warehouse_id=user.warehouse_id,
            warehouse_name=user.warehouse.name if user.warehouse else None,
            is_active=user.is_active,
            last_login_at=user.last_login_at,
        )


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "current_password": "current-password",
            "new_password": "a-new-strong-password"
    }})

    current_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)
