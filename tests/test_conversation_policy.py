import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from conftest import FakeModels
from langchain_core.messages import AIMessage, ToolMessage
from test_agent import Chat, EmptyRetriever, KnowledgeRetriever, settings

from context_agent import conversations, support
from context_agent.agent import Agent
from context_agent.config import Settings
from context_agent.conversation_policy import REPLY, SCHEMAS, SEARCH
from context_agent.models import Models
from context_agent.schemas import ChatInput
from super_admin.businesses.models import Business


def call(name, args, id="call_1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


def reply(purpose="clarification", text="Which service do you mean?"):
    return call(REPLY, {"purpose": purpose, "reply": text})


def models_for(*responses):
    models = FakeModels()
    models.chat = Chat(responses)
    models.chat.ainvoke = AsyncMock(wraps=models.chat.ainvoke)
    models.query = AsyncMock(return_value=[1.0, 0.0, 0.0])
    return models


@pytest.mark.parametrize(
    "message,purpose,answer",
    [
        ("Hii", "social", "Hi! How can I help?"),
        ("Thanks", "social", "You're welcome!"),
        ("வணக்கம்", "social", "வணக்கம்! எப்படி உதவலாம்?"),
        (
            "who is cm",
            "out_of_scope",
            "I can help with clinic enquiries. What would you like to know?",
        ),
        ("Teeth pain", "clarification", "Would you like to arrange a consultation?"),
        ("naalaiku", "clarification", "Entha neram venum?"),
        ("yes", "clarification", "What is your name?"),
        ("1", "clarification", "What would you like to consult about?"),
    ],
)
async def test_non_factual_replies_need_no_knowledge_or_ticket(db, message, purpose, answer):
    models = models_for(reply(purpose, answer))
    agent = Agent(db, models, EmptyRetriever(), settings())
    async with db.transaction("dental") as session:
        conversation_id = await conversations.get_or_create(session, "dental", "web", "patient")
        await conversations.record_message(
            session, "dental", conversation_id, "assistant", "What is your dental concern?"
        )
    result = await agent.run(
        "dental", ChatInput(message=message, request_id=uuid4(), external_user_id="patient")
    )
    assert result["outcome"] == "answered"
    assert result["answer"] == answer
    assert result["tool_results"] == result["knowledge_units"] == []
    assert models.chat.ainvoke.await_count == models.query.await_count == 1
    assert models.chat.last_messages[-2].content == "What is your dental concern?"
    async with db.transaction("dental") as session:
        assert await support.list_tickets(session, "dental") == []
        history = await conversations.load_history(session, "dental", conversation_id, 10)
    assert history[-2:] == [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ]


async def test_scope_redirect_still_applies_when_retrieval_has_unrelated_hits(db):
    models = models_for(reply("out_of_scope", "I can help with business enquiries."))
    result = await Agent(db, models, KnowledgeRetriever(), settings()).run(
        "dental", ChatInput(message="Who is CM?", request_id=uuid4())
    )
    assert result["outcome"] == "answered" and result["tool_results"] == []
    assert result["answer"] == "I can help with business enquiries."


@pytest.mark.parametrize("control", [reply(), call(SEARCH, {"query": "consultation price"})])
async def test_mixed_control_and_action_executes_neither(db, control):
    control.tool_calls.append(
        {
            "name": "create_support_ticket",
            "args": {"question": "irrelevant", "reason": "no evidence"},
            "id": "support_1",
            "type": "tool_call",
        }
    )
    models = models_for(control, reply())
    result = await Agent(db, models, EmptyRetriever(), settings()).run(
        "a", ChatInput(message="What?", request_id=uuid4())
    )
    assert result["outcome"] == "answered" and result["tool_results"] == []
    assert models.query.await_count == 1
    errors = [
        json.loads(m.content) for m in models.chat.last_messages if isinstance(m, ToolMessage)
    ]
    assert len(errors) == 2 and all(not e["ok"] for e in errors)
    async with db.transaction("a") as session:
        assert await support.list_tickets(session, "a") == []


@pytest.mark.parametrize(
    "args",
    [
        {"purpose": "factual_answer", "reply": "Costs 9999."},
        {"purpose": "social", "reply": "   "},
        {"purpose": "clarification", "reply": "Which?", "tenant_id": "another"},
    ],
)
async def test_invalid_reply_is_rejected_before_terminal_route(db, args):
    models = models_for(call(REPLY, args), reply())
    result = await Agent(db, models, EmptyRetriever(), settings()).run(
        "a", ChatInput(message="What?", request_id=uuid4())
    )
    assert result["answer"] == "Which service do you mean?"
    assert models.chat.ainvoke.await_count == 2
    error = json.loads(models.chat.last_messages[-1].content)
    assert error["ok"] is False


class RetryRetriever(EmptyRetriever):
    def __init__(self):
        self.calls = []
        self.row = SimpleNamespace(id=uuid4(), title="Hours", content="Open Monday to Saturday.")

    async def search(self, session, tenant, query, kind="knowledge_units", **kwargs):
        self.calls.append((tenant, query, kind))
        return [self.row] if query == "clinic opening hours" and kind == "knowledge_units" else []


async def test_contextual_search_recovers_related_wording_in_same_tenant(db):
    retriever = RetryRetriever()
    models = models_for(
        call(SEARCH, {"query": "clinic opening hours"}), AIMessage(content=retriever.row.content)
    )
    result = await Agent(db, models, retriever, settings()).run(
        "dental", ChatInput(message="When open", request_id=uuid4())
    )
    assert result["outcome"] == "answered" and result["tool_results"] == []
    assert result["knowledge_units"] == [
        {"id": str(retriever.row.id), "title": "Hours", "content": retriever.row.content}
    ]
    assert retriever.calls == [
        ("dental", "When open", "knowledge_units"),
        ("dental", "When open", "tools"),
        ("dental", "clinic opening hours", "knowledge_units"),
    ]
    assert models.query.await_count == models.chat.ainvoke.await_count == 2
    assert (
        json.loads(models.chat.last_messages[-1].content)["knowledge"] == result["knowledge_units"]
    )


async def test_empty_search_cannot_be_used_as_supporting_evidence(db):
    models = models_for(call(SEARCH, {"query": "price"}), AIMessage(content="Costs 9999."))
    result = await Agent(db, models, EmptyRetriever(), settings()).run(
        "a", ChatInput(message="price?", request_id=uuid4())
    )
    assert result["outcome"] == "escalated" and "9999" not in result["answer"]
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["name"] == "create_support_ticket"


async def test_retry_is_bounded_and_terminal_reply_works_at_round_limit(db):
    models = models_for(
        call(SEARCH, {"query": "opening"}), call(SEARCH, {"query": "hours"}), reply()
    )
    result = await Agent(db, models, EmptyRetriever(), settings()).run(
        "a", ChatInput(message="when?", request_id=uuid4())
    )
    assert result["outcome"] == "answered" and result["tool_results"] == []
    assert models.query.await_count == 2  # initial retrieval plus just one retry
    assert models.chat.ainvoke.await_count == 3
    assert "already retried" in models.chat.last_messages[-1].content


async def test_retry_rejects_cross_tenant_arguments(db):
    models = models_for(call(SEARCH, {"query": "fees", "tenant": "victim"}), reply())
    result = await Agent(db, models, EmptyRetriever(), settings()).run(
        "a", ChatInput(message="when?", request_id=uuid4())
    )
    assert result["outcome"] == "answered" and models.query.await_count == 1


async def test_business_scope_is_tenant_specific_and_present_without_knowledge(db):
    async with db.transaction() as session:
        session.add_all(
            [
                Business(slug="clinic", name="Example Dental", description="Dental appointments"),
                Business(slug="shop", name="Example Books", description="Book orders and delivery"),
            ]
        )
    models = models_for()
    agent = Agent(db, models, EmptyRetriever(), settings())
    state = await agent.retrieve(
        {"tenant": "shop", "request": ChatInput(message="Hi", request_id=uuid4())}
    )
    assert "Example Books" in state["dynamic_context"]
    assert "Book orders and delivery" in state["dynamic_context"]
    assert "Example Dental" not in state["dynamic_context"]
    assert {schema["function"]["name"] for schema in state["schemas"]} >= {REPLY, SEARCH}


@pytest.mark.parametrize("provider", ["gemini", "deepseek", "deepinfra"])
async def test_conversation_schemas_bind_to_all_supported_chat_providers(provider):
    models = Models(
        Settings(
            _env_file=None,
            gemini_api_key="placeholder",
            llm_provider=provider,
            llm_api_key="placeholder",
            chat_model="gemini-3.8-flash" if provider == "gemini" else "chat",
        )
    )
    try:
        bound = models.chat.bind_tools(SCHEMAS)
        assert bound is not None
        if provider == "gemini":
            from langchain_core.messages import HumanMessage

            request = models.chat._prepare_request([HumanMessage("Hi")], tools=SCHEMAS)
            declarations = request["config"].tools[0].function_declarations
            assert {d.name for d in declarations} == {REPLY, SEARCH}
        else:
            assert {s["function"]["name"] for s in bound.kwargs["tools"]} == {REPLY, SEARCH}
    finally:
        await models.close()
