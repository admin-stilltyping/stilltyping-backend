from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from context_agent.notifications import queue_notification
from context_agent.schemas import DomainError
from crm.service import fingerprint, owned, retry_result, transaction_customer
from custom_fields.service import Owner
from modules.service import module_transaction
from products.models import Product

from .models import Order
from .schemas import OrderCreate, OrderOutput, OrderUpdate

router = APIRouter(prefix="/admin/{slug}/orders", tags=["Orders"])


@router.get("")
async def orders(
    identity: Owner,
    request: Request,
    customer_id: UUID | None = None,
    status: Literal["all", "confirmed", "fulfilled", "cancelled"] = "all",
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "orders"
    ) as session:
        filters = [Order.business_id == identity.business.id]
        if customer_id:
            filters.append(Order.customer_id == customer_id)
        if status != "all":
            filters.append(Order.status == status)
        rows = (
            await session.scalars(
                select(Order)
                .where(*filters)
                .order_by(Order.created_at.desc(), Order.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {
            "items": [OrderOutput.model_validate(row) for row in rows],
            "total": await session.scalar(select(func.count()).select_from(Order).where(*filters)),
        }


@router.get("/{order_id}", response_model=OrderOutput)
async def detail(order_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "orders"
    ) as session:
        return await owned(session, Order, identity.business.id, order_id)


@router.post("", response_model=OrderOutput, status_code=201)
async def create(payload: OrderCreate, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "orders"
    ) as session:
        previous = await retry_result(session, Order, identity.business.id, payload)
        if previous:
            return previous
        if len({item.product_id for item in payload.items}) != len(payload.items):
            raise DomainError(
                400,
                "duplicate_product",
                "Each product must appear once. Change its quantity instead.",
            )
        items, currencies, total = [], set(), Decimal("0.00")
        for item in payload.items:
            product = await owned(session, Product, identity.business.id, item.product_id)
            if product.status != "active":
                raise DomainError(
                    400, "product_unavailable", f"{product.name} is not available for new orders."
                )
            line_total = product.price * item.quantity
            total += line_total
            currencies.add(product.currency)
            items.append(
                {
                    "product_id": str(product.id),
                    "product_name": product.name,
                    "product_sku": product.sku,
                    "unit_price": str(product.price),
                    "quantity": item.quantity,
                    "total": str(line_total),
                }
            )
        if len(currencies) != 1 or total > Decimal("999999999999.99"):
            raise DomainError(
                400,
                "invalid_total",
                "Choose products in one currency and keep the total below 1 trillion.",
            )
        customer = await transaction_customer(session, identity.business, payload)
        order_id = uuid4()
        row = Order(
            id=order_id,
            business_id=identity.business.id,
            customer_id=customer.id,
            lead_id=payload.lead_id,
            request_id=payload.request_id,
            request_hash=fingerprint(payload),
            reference="ORD-" + order_id.hex,
            items=items,
            currency=currencies.pop(),
            total=total,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        await queue_notification(session, identity.business.id, "order", row.id, f"/orders/{row.id}")
        return row


@router.patch("/{order_id}", response_model=OrderOutput)
async def update(order_id: UUID, payload: OrderUpdate, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "orders"
    ) as session:
        row = await owned(session, Order, identity.business.id, order_id)
        if row.status != "confirmed" and row.status != payload.status:
            raise DomainError(
                409, "invalid_transition", "A fulfilled or cancelled order cannot be changed."
            )
        row.status = payload.status
        await session.flush()
        await session.refresh(row)
        return row
