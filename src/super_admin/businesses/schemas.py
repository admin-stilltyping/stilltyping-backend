from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

from super_admin.schemas import Username

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Slug = Annotated[
    str, StringConstraints(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
]
Plan = Literal["free", "starter", "pro", "enterprise"]
Status = Literal["active", "suspended", "inactive"]
RESERVED_SLUGS = frozenset(
    {
        "www",
        "app",
        "admin",
        "super-admin",
        "superadmin",
        "api",
        "auth",
        "login",
        "signup",
        "dashboard",
        "static",
        "assets",
        "mail",
        "support",
        "staging",
        "localhost",
        "general",
        "pending",
        "suggest-slug",
        "plans",
    }
)


class BusinessCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    slug: Slug | None = None
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)] | None
    ) = None
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = (
        "Asia/Kolkata"
    )
    plan: Plan = "free"

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value):
        if value in RESERVED_SLUGS:
            raise ValueError("This subdomain is reserved.")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a valid timezone, such as Asia/Kolkata.") from None
        return value


class BusinessOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID = Field(serialization_alias="_id")
    slug: str
    name: str
    description: str | None
    timezone: str
    status: Status
    plan: Plan | None
    created_at: datetime
    updated_at: datetime


class BusinessCreated(BusinessOutput):
    admin_username: str
    admin_password: str


class BusinessBranding(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID = Field(serialization_alias="_id")
    slug: str
    name: str


class BusinessLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_slug: Slug
    username: Username
    password: SecretStr


class BusinessLoginOutput(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    business_id: UUID
    business_slug: str
    business_name: str
    username: str


class BusinessPlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Plan | None


class BusinessStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status
