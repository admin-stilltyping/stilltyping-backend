import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from custom_fields.schemas import InputModel

SocialPlatform = Literal["whatsapp", "instagram", "telegram", "facebook"]
ExternalId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class SocialInput(InputModel):
    platform: SocialPlatform
    external_id: ExternalId

    @field_validator("external_id")
    @classmethod
    def no_controls(cls, value):
        if any(ord(char) < 32 for char in value):
            raise ValueError("Social ID cannot contain control characters.")
        return value


class ContactInput(InputModel):
    phone: str | None = Field(None, max_length=40)
    social_identities: list[SocialInput] = Field(default_factory=list, max_length=10)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value):
        if not value or not value.strip():
            return None
        value = re.sub(r"[\s().-]", "", value)
        if not re.fullmatch(r"\+[1-9]\d{6,14}", value):
            raise ValueError("Use an international phone number, for example +919876543210.")
        return value

    @model_validator(mode="after")
    def unique_identities(self):
        keys = {(item.platform, item.external_id) for item in self.social_identities}
        if len(keys) != len(self.social_identities):
            raise ValueError("Duplicate social identities.")
        return self


class EnquiryInput(InputModel):
    request_id: UUID
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60000)
    ]
    channel: Literal["manual", "web", "whatsapp", "instagram", "telegram", "facebook"] = "manual"
    contact: ContactInput = Field(default_factory=ContactInput)
    lead_id: UUID | None = None


class TransactionInput(InputModel):
    request_id: UUID
    customer_id: UUID | None = None
    lead_id: UUID | None = None
    contact: ContactInput | None = None

    @model_validator(mode="after")
    def valid_recipient(self):
        if self.customer_id and (self.lead_id or self.contact is not None):
            raise ValueError("Choose a customer or a lead/contact, not both.")
        if not self.customer_id and not self.lead_id and self.contact is None:
            raise ValueError("Select a lead, customer, or supply contact details.")
        return self
