from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from custom_fields.schemas import InputModel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10000)]
Price = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]
Status = Literal["active", "inactive", "out_of_stock", "archived"]


class ProductCreate(InputModel):
    name: Name
    price: Price
    currency: Literal["INR", "USD"] = "INR"
    sku: ShortText | None = None
    category: ShortText | None = None
    description: Description | None = None
    status: Status = "active"
    attributes: dict[str, Any] = Field(default_factory=dict, max_length=200)

    @field_validator("sku", "category", "description")
    @classmethod
    def blank_to_null(cls, value):
        return value or None


class ProductUpdate(InputModel):
    name: Name | None = None
    price: Price | None = None
    sku: ShortText | None = None
    category: ShortText | None = None
    description: Description | None = None
    status: Status | None = None
    attributes: dict[str, Any] | None = Field(default=None, max_length=200)

    @field_validator("sku", "category", "description")
    @classmethod
    def blank_to_null(cls, value):
        return value or None

    @model_validator(mode="after")
    def no_required_nulls(self):
        for key in self.model_fields_set & {"name", "price", "status", "attributes"}:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null.")
        return self


class ProductOutput(ProductCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    business_id: UUID
    created_at: datetime
    updated_at: datetime


class ProductPage(InputModel):
    items: list[ProductOutput]
    total: int
    catalog_total: int
    categories: list[str]
