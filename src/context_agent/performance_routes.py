"""Owner-only, read-only PostgreSQL diagnostics for portal startup queries."""

import json
from time import perf_counter

from fastapi import APIRouter, Request
from sqlalchemy import func, select, text

from custom_fields.models import FieldDefinition
from custom_fields.service import Owner
from modules.models import BusinessModules
from services.models import Service
from super_admin.businesses.models import Business, BusinessAdmin

from .schemas import DomainError

router = APIRouter(tags=["Business diagnostics"])


async def explain_select(session, dialect, label, query):
    # Only the fixed SELECT expressions below are accepted, never client SQL.
    # Literal values come from the verified owner's UUIDs; the compiler quotes them.
    sql = str(query.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    started = perf_counter()
    plan = await session.scalar(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql))
    elapsed = (perf_counter() - started) * 1000
    if isinstance(plan, str):
        plan = json.loads(plan)
    planning = float(plan[0]["Planning Time"])
    execution = float(plan[0]["Execution Time"])
    return {
        "query": label,
        "round_trip_ms": round(elapsed, 2),
        "postgres_planning_ms": planning,
        "postgres_execution_ms": execution,
        # Includes transport and driver overhead, not a pure network measurement.
        "transport_driver_remainder_ms": round(max(0, elapsed - planning - execution), 2),
    }


@router.get("/admin/{slug}/diagnostics/database")
async def database_diagnostics(identity: Owner, request: Request):
    db = request.app.state.services["db"]
    if db.engine.dialect.name != "postgresql":
        raise DomainError(400, "postgresql_required", "These diagnostics require PostgreSQL.")
    business_id = identity.business.id
    queries = [
        ("connection_baseline", select(1)),
        ("admin_lookup", select(BusinessAdmin).where(BusinessAdmin.id == identity.account.id)),
        ("business_lookup", select(Business).where(Business.id == business_id)),
        (
            "module_permissions",
            select(BusinessModules).where(BusinessModules.business_id == business_id),
        ),
        (
            "custom_fields",
            select(FieldDefinition)
            .where(
                FieldDefinition.business_id == business_id,
                FieldDefinition.entity_type == "service",
                FieldDefinition.archived.is_(False),
            )
            .order_by(FieldDefinition.sort_order, FieldDefinition.key),
        ),
        (
            "services_list",
            select(Service)
            .where(Service.business_id == business_id)
            .order_by(Service.created_at.desc(), Service.id.desc())
            .limit(25)
            .offset(0),
        ),
        (
            "services_count",
            select(func.count()).select_from(Service).where(Service.business_id == business_id),
        ),
    ]
    started = perf_counter()
    async with db.transaction() as session:
        # Explicitly separate acquisition from the EXPLAIN round trips.
        await session.connection()
        acquire_ms = (perf_counter() - started) * 1000
        results = [
            await explain_select(session, db.engine.dialect, label, query)
            for label, query in queries
        ]
    return {
        "connection_acquire_ms": round(acquire_ms, 2),
        "queries": results,
        "measurement": "read-only EXPLAIN ANALYZE; transport remainder includes driver overhead",
    }
