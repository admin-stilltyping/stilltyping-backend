from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from crm.schemas import TransactionInput
from custom_fields.schemas import InputModel


class OrderItemInput(InputModel):
    product_id: UUID
    quantity: int = Field(ge=1, le=10000, strict=True)


class OrderCreate(TransactionInput):
    items: list[OrderItemInput] = Field(min_length=1, max_length=100)


class OrderUpdate(InputModel):
    status: Literal["fulfilled", "cancelled"]


class OrderOutput(InputModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    business_id: UUID
    customer_id: UUID
    lead_id: UUID | None
    reference: str
    status: str
    currency: str
    total: Decimal
    items: list[dict]
    created_at: datetime
    updated_at: datetime
