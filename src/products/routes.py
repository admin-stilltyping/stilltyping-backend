from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, or_, select

from context_agent.schemas import DomainError
from custom_fields.service import Owner, definitions, validate_attributes
from modules.service import module_transaction

from .models import Product
from .schemas import ProductCreate, ProductOutput, ProductPage, ProductUpdate, Status

router = APIRouter(prefix="/admin/{slug}/products", tags=["Products"])


async def get_product(session, business_id, product_id):
    row = await session.scalar(
        select(Product).where(
            Product.business_id == business_id,
            Product.id == product_id,
        )
    )
    if row is None:
        raise DomainError(404, "product_not_found", "Product not found.")
    return row


async def check_sku(session, business_id, sku, product_id=None):
    if sku is None:
        return
    query = select(Product.id).where(Product.business_id == business_id, Product.sku == sku)
    if product_id:
        query = query.where(Product.id != product_id)
    if await session.scalar(query):
        raise DomainError(
            409, "sku_exists", "A product with this SKU already exists in this business."
        )


@router.get("", response_model=ProductPage)
async def list_products(
    identity: Owner,
    request: Request,
    status: Status | Literal["all"] = "all",
    category: str | None = Query(None, max_length=100),
    search: str = Query("", max_length=200),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    scope = Product.business_id == identity.business.id
    filters = [scope]
    if status != "all":
        filters.append(Product.status == status)
    if category:
        filters.append(Product.category == category)
    if search.strip():
        filters.append(
            or_(
                *[
                    column.icontains(search.strip(), autoescape=True)
                    for column in (Product.name, Product.sku, Product.description)
                ]
            )
        )
    async with module_transaction(
        request.app.state.services["db"], identity.business, "products"
    ) as session:
        items = list(
            (
                await session.scalars(
                    select(Product)
                    .where(*filters)
                    .order_by(Product.created_at.desc(), Product.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        total = await session.scalar(select(func.count()).select_from(Product).where(*filters))
        catalog_total = await session.scalar(select(func.count()).select_from(Product).where(scope))
        categories = list(
            (
                await session.scalars(
                    select(Product.category)
                    .where(scope, Product.category.is_not(None))
                    .distinct()
                    .order_by(Product.category)
                )
            ).all()
        )
        return ProductPage(
            items=items, total=total, catalog_total=catalog_total, categories=categories
        )


@router.get("/{product_id}", response_model=ProductOutput)
async def detail(product_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "products"
    ) as session:
        return await get_product(session, identity.business.id, product_id)


@router.post("", response_model=ProductOutput, status_code=201)
async def create(payload: ProductCreate, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "products"
    ) as session:
        fields = await definitions(session, identity.business.id, "product", True)
        attributes = validate_attributes(fields, payload.attributes)
        await check_sku(session, identity.business.id, payload.sku)
        row = Product(
            **{**payload.model_dump(), "attributes": attributes}, business_id=identity.business.id
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


@router.patch("/{product_id}", response_model=ProductOutput)
async def update(product_id: UUID, payload: ProductUpdate, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "products"
    ) as session:
        row = await get_product(session, identity.business.id, product_id)
        fields = await definitions(session, identity.business.id, "product", True)
        changes = payload.model_dump(exclude_unset=True)
        changes["attributes"] = validate_attributes(
            fields, changes.get("attributes", {}), row.attributes
        )
        await check_sku(session, identity.business.id, changes.get("sku", row.sku), row.id)
        for key, value in changes.items():
            setattr(row, key, value)
        await session.flush()
        await session.refresh(row)
        return row


@router.delete("/{product_id}", status_code=204)
async def archive(product_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "products"
    ) as session:
        row = await get_product(session, identity.business.id, product_id)
        row.status = "archived"
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
