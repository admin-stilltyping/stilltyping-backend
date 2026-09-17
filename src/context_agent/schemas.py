from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60000)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentInput(StrictModel):
    title: ShortText
    summary: Text


class AddInput(StrictModel):
    content: Text


class UpdateInput(StrictModel):
    change: Text


class UnitDraft(StrictModel):
    title: ShortText
    content: Text


class Extraction(StrictModel):
    units: list[UnitDraft] = Field(max_length=100)
    clarification: str = ""


class UnitEdit(UnitDraft):
    id: UUID


class UpdatePlan(StrictModel):
    outcome: Literal["update", "unchanged", "not_found", "clarification"]
    edits: list[UnitEdit] = Field(default_factory=list, max_length=100)
    clarification: str = ""


class TicketUpdate(StrictModel):
    status: Literal["open", "resolved"] | None = None
    notes: Text | None = None


class InstructionsInput(StrictModel):
    # Empty string is allowed, to clear a tenant's instructions.
    instructions: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20000)]


class ChatInput(StrictModel):
    message: Text
    request_id: UUID
    channel: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)
    ] = "web"
    external_user_id: ShortText | None = None


class KnowledgeContext(StrictModel):
    id: UUID
    title: str
    content: str


class ChatOutput(StrictModel):
    request_id: UUID
    outcome: Literal["answered", "escalated", "escalation_failed"]
    answer: str
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_units: list[KnowledgeContext] = Field(
        default_factory=list, description="Exact retrieved knowledge supplied to the answer LLM."
    )
    conversation_id: UUID | None = Field(
        default=None,
        description="Conversation id when the request supplies external_user_id. Reuse it (or the "
        "same channel + external_user_id) to continue the thread; null for stateless requests.",
    )
    response_time_ms: float | None = Field(
        default=None,
        ge=0,
        description="Server processing time in milliseconds, including embeddings, retrieval and "
        "answer generation. Excludes response transmission and client network latency.",
    )
    usage: dict[str, Any] | None = Field(
        default=None, description="Per-request provider-reported LLM and embedding token counts."
    )


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str, **details):
        self.status, self.code, self.message, self.details = status, code, message, details
        super().__init__(message)
