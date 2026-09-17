from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints

Username = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$"
    ),
]


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Username
    password: SecretStr


class LoginOutput(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class SuperAdminProfile(BaseModel):
    id: UUID
    username: str
    role: Literal["super_admin"] = "super_admin"
