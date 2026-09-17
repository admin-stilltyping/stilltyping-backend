from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from context_agent.knowledge_document import document_snapshot
from context_agent.schemas import DocumentInput, StrictModel, Text
from super_admin.businesses.auth import BusinessIdentity, require_business_admin
from super_admin.routes import no_cache

router = APIRouter(prefix="/admin/knowledge-base", tags=["Knowledge base"])
Identity = Annotated[BusinessIdentity, Depends(require_business_admin)]


class KnowledgeInput(StrictModel):
    content: Text
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class KnowledgeOutput(StrictModel):
    content: str
    revision: str
    reconstructed: bool


@router.get("", response_model=KnowledgeOutput)
async def read_knowledge(request: Request, response: Response, identity: Identity):
    no_cache(response)
    async with request.app.state.services["db"].transaction(identity.business.slug) as session:
        return await document_snapshot(session, identity.business.slug)


@router.put("", response_model=KnowledgeOutput)
async def save_knowledge(
    payload: KnowledgeInput, request: Request, response: Response, identity: Identity
):
    no_cache(response)
    result, _ = await request.app.state.services["knowledge"].put(
        identity.business.slug,
        DocumentInput(title=f"{identity.business.name} knowledge base", summary=payload.content),
        expected_revision=payload.expected_revision,
    )
    return result["editor"]
