import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from appointments.routes import router as appointments_router
from crm.routes import router as crm_router
from custom_fields.routes import router as custom_fields_router
from dashboard.routes import router as dashboard_router
from integrations.routes import router as integrations_router
from knowledge_base.routes import router as knowledge_base_router
from modules.dependencies import require_support_owner
from modules.routes import management as module_management_router
from modules.routes import portal as module_portal_router
from orders.routes import router as orders_router
from products.routes import router as products_router
from public_chat.routes import router as public_chat_router
from services.routes import router as services_router
from super_admin.businesses.routes import management as business_management_router
from super_admin.businesses.routes import portal as business_portal_router
from super_admin.errors import AuthError, auth_error_response
from super_admin.routes import router as super_admin_router

from . import instructions, support
from .agent import Agent
from .ai_usage import router as ai_usage_router
from .config import Settings
from .db import Database
from .knowledge import KnowledgeService
from .models import Models
from .portal_chat import router as portal_chat_router
from .portal_webhooks import router as portal_webhooks_router
from .retrieval import Retriever
from .schemas import (
    AddInput,
    ChatInput,
    ChatOutput,
    DocumentInput,
    DomainError,
    InstructionsInput,
    TicketUpdate,
    UpdateInput,
)
from .usage import UsageMiddleware, response_timing
from .vectors import Vectors
from .webhooks import register_webhooks

log = logging.getLogger(__name__)


def create_app(services=None):
    @asynccontextmanager
    async def lifespan(app):
        if services is not None:
            app.state.services = services
            yield
            return
        settings = Settings()
        db, vectors = Database(settings.database_url), Vectors(settings)
        models = None
        try:
            models = Models(settings)
            retriever = Retriever(models, vectors, settings)
            app.state.services = {
                "db": db,
                "vectors": vectors,
                "settings": settings,
                "knowledge": KnowledgeService(db, models, vectors, retriever),
                "agent": Agent(db, models, retriever, settings),
            }
            yield
        finally:
            if models is not None:
                await models.close()
            await vectors.close()
            await db.close()

    app = FastAPI(title="Context Agent", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(AuthError, auth_error_response)
    app.include_router(super_admin_router)
    app.include_router(business_management_router)
    app.include_router(business_portal_router)
    app.include_router(dashboard_router)
    app.include_router(knowledge_base_router)
    app.include_router(custom_fields_router)
    app.include_router(products_router)
    app.include_router(module_management_router)
    app.include_router(module_portal_router)
    app.include_router(crm_router)
    app.include_router(services_router)
    app.include_router(orders_router)
    app.include_router(appointments_router)
    app.include_router(public_chat_router)
    app.include_router(integrations_router)
    app.include_router(portal_chat_router)
    app.include_router(ai_usage_router)
    app.include_router(portal_webhooks_router)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message, **exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request fields are missing or invalid.",
                }
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request, exc):
        log.error("Database request failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "storage_unavailable",
                    "message": "Database temporarily unavailable.",
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        log.error("Request failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=502,
            content={
                "error": {"code": "processing_failed", "message": "Request processing failed."},
                **response_timing(request.scope.get("state", {})),
                **(
                    {"usage": request.state.token_usage.as_dict()}
                    if hasattr(request.state, "token_usage")
                    else {}
                ),
            },
        )

    @app.middleware("http")
    async def bound_request(request, call_next):
        tenant_key = getattr(app.state.services["settings"], "tenant_api_key", None)
        if tenant_key and request.url.path.startswith("/api/v1/tenants/"):
            supplied = request.headers.get("x-tenant-api-key", "")
            if not secrets.compare_digest(
                supplied.encode(), tenant_key.get_secret_value().encode()
            ):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"code": "unauthenticated", "message": "Invalid API key."}},
                )
        # Input size is checked before parsing to avoid unbounded upload memory usage.
        if request.method in ("PUT", "POST", "PATCH"):
            data = bytearray()
            async for part in request.stream():
                data.extend(part)
                if len(data) > 300000:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": {
                                "code": "validation_error",
                                "message": "Request body is too large.",
                            }
                        },
                    )
            request._body = bytes(data)
        try:
            async with asyncio.timeout(app.state.services["settings"].request_timeout):
                return await call_next(request)
        except TimeoutError:
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "code": "processing_failed",
                        "message": "Processing timed out. Check state before retrying mutations.",
                    }
                },
            )

    def scope(tenant):
        if tenant == "general":
            raise DomainError(400, "validation_error", "general is reserved for shared tools.")
        return tenant

    tenant_path = Path(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

    @app.put("/api/v1/tenants/{tenant_id}/document")
    async def put_document(
        payload: DocumentInput, request: Request, response: Response, tenant_id: str = tenant_path
    ):
        result, created = await request.app.state.services["knowledge"].put(
            scope(tenant_id), payload
        )
        response.status_code = 201 if created else 200
        return result

    @app.post("/api/v1/tenants/{tenant_id}/document/knowledge-units", status_code=201)
    async def add_knowledge(payload: AddInput, request: Request, tenant_id: str = tenant_path):
        return await request.app.state.services["knowledge"].add(scope(tenant_id), payload)

    @app.patch("/api/v1/tenants/{tenant_id}/document/knowledge-units")
    async def update_knowledge(
        payload: UpdateInput, request: Request, tenant_id: str = tenant_path
    ):
        return await request.app.state.services["knowledge"].update(scope(tenant_id), payload)

    @app.post("/api/v1/tenants/{tenant_id}/agent/messages", response_model=ChatOutput)
    async def message(payload: ChatInput, request: Request, tenant_id: str = tenant_path):
        return await request.app.state.services["agent"].run(scope(tenant_id), payload)

    # Portal aliases use the same business-owner JWT checks as the legacy routes.
    # Keep the /api/v1 tenant-key middleware unchanged for server integrations.
    @app.get(
        "/admin/{tenant_id}/support-tickets", dependencies=[Depends(require_support_owner)]
    )
    @app.get(
        "/api/v1/tenants/{tenant_id}/support-tickets", dependencies=[Depends(require_support_owner)]
    )
    async def list_support_tickets(
        request: Request, status: str | None = None, tenant_id: str = tenant_path
    ):
        tenant = scope(tenant_id)
        async with request.app.state.services["db"].transaction(tenant) as session:
            rows = await support.list_tickets(session, tenant, status)
        return {"tickets": [support.as_dict(row) for row in rows]}

    @app.get(
        "/admin/{tenant_id}/support-tickets/{ticket_ref}",
        dependencies=[Depends(require_support_owner)],
    )
    @app.get(
        "/api/v1/tenants/{tenant_id}/support-tickets/{ticket_ref}",
        dependencies=[Depends(require_support_owner)],
    )
    async def get_support_ticket(request: Request, ticket_ref: str, tenant_id: str = tenant_path):
        tenant = scope(tenant_id)
        async with request.app.state.services["db"].transaction(tenant) as session:
            row = await support.get_ticket(session, tenant, ticket_ref)
        if row is None:
            raise DomainError(404, "ticket_not_found", "No support ticket with that reference.")
        return support.as_dict(row)

    @app.patch(
        "/admin/{tenant_id}/support-tickets/{ticket_ref}",
        dependencies=[Depends(require_support_owner)],
    )
    @app.patch(
        "/api/v1/tenants/{tenant_id}/support-tickets/{ticket_ref}",
        dependencies=[Depends(require_support_owner)],
    )
    async def update_support_ticket(
        payload: TicketUpdate, request: Request, ticket_ref: str, tenant_id: str = tenant_path
    ):
        tenant = scope(tenant_id)
        async with request.app.state.services["db"].transaction(tenant) as session:
            row = await support.update_ticket(
                session, tenant, ticket_ref, status=payload.status, notes=payload.notes
            )
            if row is None:
                raise DomainError(404, "ticket_not_found", "No support ticket with that reference.")
            data = support.as_dict(row)
        return data

    @app.put("/admin/{tenant_id}/instructions", dependencies=[Depends(require_support_owner)])
    @app.put("/api/v1/tenants/{tenant_id}/instructions")
    async def put_instructions(
        payload: InstructionsInput, request: Request, tenant_id: str = tenant_path
    ):
        tenant = scope(tenant_id)
        async with request.app.state.services["db"].transaction(tenant) as session:
            await instructions.set_instructions(session, tenant, payload.instructions)
        return {"tenant_id": tenant, "length": len(payload.instructions)}

    @app.get("/admin/{tenant_id}/instructions", dependencies=[Depends(require_support_owner)])
    @app.get("/api/v1/tenants/{tenant_id}/instructions")
    async def read_instructions(request: Request, tenant_id: str = tenant_path):
        tenant = scope(tenant_id)
        async with request.app.state.services["db"].transaction(tenant) as session:
            text = await instructions.get_instructions(session, tenant)
        return {"tenant_id": tenant, "instructions": text}

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        svc = request.app.state.services
        async with svc["db"].transaction() as session:
            await session.execute(text("SELECT 1"))
        for kind in ("knowledge_units", "tools"):
            await svc["vectors"].client.get_collection(svc["vectors"].collection(kind))
        return {"status": "ready"}

    # Inbound social webhooks live outside /api/ so UsageMiddleware never rewrites
    # their non-JSON (Meta challenge) or empty-ack responses.
    register_webhooks(app)

    app.add_middleware(UsageMiddleware)
    return app


app = create_app()
