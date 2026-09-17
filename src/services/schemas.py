from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from custom_fields.schemas import InputModel
from products.schemas import Description, Name, Price, ShortText

ServiceStatus = Literal["active", "inactive", "archived"]


class ServiceCreate(InputModel):
    name: Name
    price: Price
    currency: Literal["INR", "USD"] = "INR"
    duration_minutes: int = Field(30, ge=1, le=1440, strict=True)
    description: Description | None = None
    category: ShortText | None = None
    status: ServiceStatus = "active"
    custom_fields: dict[str, Any] = Field(default_factory=dict, max_length=200)

    @field_validator("category", "description")
    @classmethod
    def blank_to_null(cls, value):
        return value or None


class ServiceUpdate(InputModel):
    name: Name | None = None
    price: Price | None = None
    duration_minutes: int | None = Field(None, ge=1, le=1440, strict=True)
    description: Description | None = None
    category: ShortText | None = None
    status: ServiceStatus | None = None
    custom_fields: dict[str, Any] | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def no_nulls(self):
        for key in self.model_fields_set - {"description", "category"}:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null.")
        return self


class ServiceOutput(ServiceCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    business_id: UUID
    created_at: datetime
    updated_at: datetime
