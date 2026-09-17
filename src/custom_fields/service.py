import math
import re
from datetime import date
from typing import Annotated

from fastapi import Depends, Response
from sqlalchemy import select

from context_agent.schemas import DomainError
from super_admin.businesses.auth import BusinessIdentity, require_business_admin
from super_admin.errors import AuthError
from super_admin.routes import no_cache

from .models import FieldDefinition


async def require_owner(
    slug: str,
    response: Response,
    identity: Annotated[BusinessIdentity, Depends(require_business_admin)],
) -> BusinessIdentity:
    if identity.business.slug != slug:
        raise AuthError(403, "tenant_access_denied", "You do not have access to this business.")
    no_cache(response)
    return identity


Owner = Annotated[BusinessIdentity, Depends(require_owner)]


async def definitions(session, business_id, entity_type, include_archived=False):
    query = select(FieldDefinition).where(
        FieldDefinition.business_id == business_id, FieldDefinition.entity_type == entity_type
    )
    if not include_archived:
        query = query.where(FieldDefinition.archived.is_(False))
    return list(
        (
            await session.scalars(query.order_by(FieldDefinition.sort_order, FieldDefinition.key))
        ).all()
    )


def validate_attributes(fields, incoming, existing=None):
    """Merge a PATCH; null clears an optional value. Archived values are read-only."""
    existing = existing or {}
    by_key = {field.key: field for field in fields}
    result = dict(existing)
    for key, value in incoming.items():
        field = by_key.get(key)
        if field is None:
            raise DomainError(400, "unknown_field", f"Unknown custom field: {key}.")
        if field.archived:
            if (
                key not in existing
                or value != existing[key]
                or type(value) is not type(existing[key])
            ):
                raise DomainError(
                    400, "archived_field", f"{field.label} is archived and read-only."
                )
            continue
        if value is None or value == "" or value == []:
            result.pop(key, None)
            continue
        kind = field.field_type
        valid = False
        if kind == "text":
            valid = isinstance(value, str) and len(value) <= 4000
        elif kind == "number":
            valid = type(value) in (int, float) and abs(value) <= 1e15 and math.isfinite(value)
        elif kind == "boolean":
            valid = type(value) is bool
        elif kind == "date":
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                try:
                    date.fromisoformat(value)
                    valid = True
                except ValueError:
                    pass
        elif kind in {"select", "multiselect"}:
            options = {option["value"] for option in field.options}
            if kind == "select":
                valid = isinstance(value, str) and value in options
            else:
                valid = (
                    isinstance(value, list)
                    and len(value) <= len(options)
                    and all(isinstance(item, str) and item in options for item in value)
                    and len(set(value)) == len(value)
                )
        if not valid:
            raise DomainError(
                400, "invalid_field_value", f"Invalid value for {field.label} ({kind})."
            )
        result[key] = value
    for field in fields:
        if not field.archived and field.required:
            value = result.get(field.key)
            if value is None or (isinstance(value, str) and not value.strip()) or value == []:
                raise DomainError(400, "required_field", f"{field.label} is required.")
    return result
