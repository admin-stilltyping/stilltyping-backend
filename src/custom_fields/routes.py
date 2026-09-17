from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select

from context_agent.schemas import DomainError
from modules.service import module_transaction

from .models import FieldDefinition
from .schemas import DefinitionCreate, DefinitionOutput, DefinitionUpdate, EntityType
from .service import Owner, definitions

router = APIRouter(prefix="/admin/{slug}/custom-fields/{entity_type}", tags=["Custom fields"])


async def get_field(session, business_id, entity_type, field_id):
    field = await session.scalar(
        select(FieldDefinition).where(
            FieldDefinition.business_id == business_id,
            FieldDefinition.entity_type == entity_type,
            FieldDefinition.id == field_id,
        )
    )
    if field is None:
        raise DomainError(404, "field_not_found", "Custom field not found.")
    return field


@router.get("", response_model=list[DefinitionOutput])
async def list_fields(
    entity_type: EntityType,
    identity: Owner,
    request: Request,
    include_archived: bool = Query(False),
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, entity_type + "s"
    ) as session:
        return await definitions(session, identity.business.id, entity_type, include_archived)


@router.post("", response_model=DefinitionOutput, status_code=201)
async def create_field(
    entity_type: EntityType,
    payload: DefinitionCreate,
    identity: Owner,
    request: Request,
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, entity_type + "s"
    ) as session:
        current = await definitions(session, identity.business.id, entity_type, True)
        if any(field.key == payload.key for field in current):
            raise DomainError(
                409, "field_key_exists", "This key already exists, including archived fields."
            )
        if sum(not field.archived for field in current) >= 100:
            raise DomainError(
                400, "field_limit", "A module can have at most 100 active custom fields."
            )
        if len(current) >= 200:
            raise DomainError(
                400,
                "field_limit",
                "A module can have at most 200 custom fields including archived fields.",
            )
        field = FieldDefinition(
            business_id=identity.business.id, entity_type=entity_type, **payload.model_dump()
        )
        session.add(field)
        await session.flush()
        await session.refresh(field)
        return field


@router.patch("/{field_id}", response_model=DefinitionOutput)
async def update_field(
    entity_type: EntityType,
    field_id: UUID,
    payload: DefinitionUpdate,
    identity: Owner,
    request: Request,
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, entity_type + "s"
    ) as session:
        field = await get_field(session, identity.business.id, entity_type, field_id)
        if field.archived:
            raise DomainError(409, "archived_field", "Archived fields cannot be edited.")
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("field_type", field.field_type) != field.field_type:
            raise DomainError(
                409,
                "field_type_locked",
                "Field types are fixed. Create a new field for a different type.",
            )
        values = {key: getattr(field, key) for key in DefinitionCreate.model_fields}
        try:
            merged = DefinitionCreate.model_validate({**values, **changes})
        except ValidationError:
            raise DomainError(
                400, "invalid_definition", "Check the custom field label and options."
            ) from None
        old_options = {option["value"] for option in field.options or []}
        if not old_options.issubset({option.value for option in merged.options or []}):
            raise DomainError(
                409,
                "option_in_use",
                "Existing option values must be kept. You can add options or change their labels.",
            )
        for key, value in merged.model_dump().items():
            setattr(field, key, value)
        await session.flush()
        await session.refresh(field)
        return field


@router.delete("/{field_id}", status_code=204)
async def archive_field(entity_type: EntityType, field_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, entity_type + "s"
    ) as session:
        field = await get_field(session, identity.business.id, entity_type, field_id)
        field.archived = True
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
