from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, StringConstraints, model_validator

from crm.schemas import TransactionInput
from custom_fields.schemas import InputModel

Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]


class AppointmentCreate(TransactionInput):
    service_id: UUID
    scheduled_at: AwareDatetime
    notes: Notes | None = None


class AppointmentUpdate(InputModel):
    scheduled_at: AwareDatetime | None = None
    notes: Notes | None = None
    status: Literal["confirmed", "completed", "cancelled", "no_show"] | None = None

    @model_validator(mode="after")
    def no_nulls(self):
        for key in self.model_fields_set - {"notes"}:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null.")
        return self


class AppointmentOutput(InputModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    business_id: UUID
    customer_id: UUID
    lead_id: UUID | None
    service_id: UUID
    service_name: str
    price: Decimal
    currency: str
    duration_minutes: int
    scheduled_at: datetime
    status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime
