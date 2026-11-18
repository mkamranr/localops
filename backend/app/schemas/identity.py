"""Schemas for auth, tenants, users, roles and permissions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from app.schemas.common import ApiModel

PASSWORD_MIN_LENGTH = 12


def _normalise_username(value: str) -> str:
    return value.strip().lower()


Username = Annotated[
    str,
    AfterValidator(_normalise_username),
    StringConstraints(pattern=r"^[A-Za-z0-9._-]{3,100}$"),
]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
Password = Annotated[str, StringConstraints(min_length=PASSWORD_MIN_LENGTH, max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def _lower_email(value: str) -> str:
    return value.strip().lower()


# Syntactic check only: internal / air-gapped networks commonly use reserved
# domains (.local, .internal, .lan) that strict validators reject.
Email = Annotated[
    str,
    AfterValidator(_lower_email),
    StringConstraints(strip_whitespace=True, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]


# --------------------------------------------------------------------------- auth


class LoginRequest(BaseModel):
    tenant: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True)] = "default"
    username: Annotated[
        str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=100)
    ]
    password: Annotated[str, StringConstraints(max_length=128)]


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int


class ChangePasswordRequest(BaseModel):
    current_password: Annotated[str, StringConstraints(max_length=128)]
    new_password: Password

    @model_validator(mode="after")
    def _differs(self) -> ChangePasswordRequest:
        if self.current_password == self.new_password:
            raise ValueError("New password must differ from the current password")
        return self


class TenantBrief(ApiModel):
    id: uuid.UUID
    name: str
    slug: str


class MeResponse(BaseModel):
    id: uuid.UUID
    username: str
    email: str
    display_name: str
    must_change_password: bool
    tenant: TenantBrief
    roles: list[str]
    permissions: list[str]


# --------------------------------------------------------------------------- tenants


class TenantAdminCreate(BaseModel):
    username: Username
    email: Email
    password: Password
    display_name: Name | None = None


class TenantCreate(BaseModel):
    name: Name
    slug: Slug
    admin: TenantAdminCreate


class TenantUpdate(BaseModel):
    name: Name | None = None
    status: Literal["active", "disabled"] | None = None


class TenantOut(ApiModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    updated_at: datetime | None = None


# --------------------------------------------------------------------------- users


class RoleBrief(ApiModel):
    id: uuid.UUID
    name: str


class UserCreate(BaseModel):
    username: Username
    email: Email
    password: Password
    display_name: Name | None = None
    role_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    must_change_password: bool = True


class UserUpdate(BaseModel):
    email: Email | None = None
    display_name: Name | None = None
    status: Literal["active", "disabled"] | None = None
    password: Password | None = Field(None, description="Administrative password reset")


class UserRolesUpdate(BaseModel):
    role_ids: list[uuid.UUID] = Field(max_length=20)


class UserOut(ApiModel):
    id: uuid.UUID
    username: str
    email: str
    display_name: str
    status: str
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime
    roles: list[RoleBrief] = Field(default_factory=list)


# --------------------------------------------------------------------------- roles


class PermissionOut(ApiModel):
    code: str
    description: str


class RoleOut(ApiModel):
    id: uuid.UUID
    name: str
    description: str
    is_system: bool
    permissions: list[str]
    user_count: int
