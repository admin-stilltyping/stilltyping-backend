from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

EntityType = Literal["product", "service"]
FieldType = Literal["text", "number", "boolean", "select", "multiselect", "date"]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldOption(InputModel):
    value: Label
    label: Label


class DefinitionCreate(InputModel):
    key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    label: Label
    field_type: FieldType
    options: list[FieldOption] | None = Field(default=None, max_length=100)
    required: bool = Field(default=False, strict=True)
    sort_order: int = Field(default=0, ge=-100000, le=100000, strict=True)

    @model_validator(mode="after")
    def valid_definition(self):
        if self.key in {"constructor", "prototype"}:
            raise ValueError("Reserved field key.")
        if self.field_type in {"select", "multiselect"}:
            if not self.options:
                raise ValueError("Select fields require options.")
            if len({option.value for option in self.options}) != len(self.options):
                raise ValueError("Option values must be unique.")
        elif self.options is not None:
            raise ValueError("Only select fields support options.")
        return self


class DefinitionUpdate(InputModel):
    label: Label | None = None
    field_type: FieldType | None = None
    options: list[FieldOption] | None = Field(default=None, max_length=100)
    required: bool | None = Field(default=None, strict=True)
    sort_order: int | None = Field(default=None, ge=-100000, le=100000, strict=True)

    @model_validator(mode="after")
    def no_nulls(self):
        for key in self.model_fields_set - {"options"}:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null.")
        return self


class DefinitionOutput(DefinitionCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    entity_type: EntityType
    archived: bool
