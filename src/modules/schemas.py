from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ModuleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    product_orders: StrictBool
    service_appointments: StrictBool
    customers: StrictBool
    leads: StrictBool
    support_tickets: StrictBool


class ModuleSettings(BaseModel):
    business_id: UUID
    selection: ModuleSelection
    revision: int
    updated_at: datetime | None
    updated_by: UUID | None


class ModuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: ModuleSelection
    expected_revision: int = Field(ge=0, strict=True)
