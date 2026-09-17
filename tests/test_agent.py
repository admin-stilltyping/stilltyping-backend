from collections import deque
from types import SimpleNamespace
from uuid import uuid4

from conftest import FakeModels
from langchain_core.messages import AIMessage

from context_agent.agent import Agent
from context_agent.schemas import ChatInput


class Chat:
    def __init__(self, responses):
        self.responses = deque(responses)

    def bind_tools(self, schemas):
        return self

    async def ainvoke(self, messages):
        self.last_messages = messages
        return self.responses.popleft()


class EmptyRetriever:
    async def search(self, *args, **kwargs):
        return []


def settings():
    return SimpleNamespace(max_tool_rounds=2, support_webhook_url=None)


async def test_unsupported_answer_escalates_and_creates_ticket(db):
    models = FakeModels()
    models.chat = Chat([AIMessage(content="Consultation costs 9999.")])
    agent = Agent(db, models, EmptyRetriever(), settings())
    result = await agent.run("a", ChatInput(message="How much?", request_id=uuid4()))
    assert result["outcome"] == "escalated"
    assert "9999" not in result["answer"]
    assert "TKT-" in result["answer"]
    assert result["tool_results"][0]["name"] == "create_support_ticket"


async def test_empty_answer_forces_escalation(db):
    models = FakeModels()
    models.chat = Chat([AIMessage(content="")])
    result = await Agent(db, models, EmptyRetriever(), settings()).run(
        "a", ChatInput(message="Question", request_id=uuid4())
    )
    assert result["outcome"] == "escalated" and "citations" not in result
    assert "TKT-" in result["answer"]


async def test_model_requested_support_creates_ticket(db):
    models = FakeModels()
    models.chat = Chat(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_support_ticket",
                        "args": {"question": "Unknown?", "reason": "No evidence"},
                        "id": "call_1",
                    }
                ],
            )
        ]
    )
    result = await Agent(db, models, EmptyRetriever(), settings()).run(
        "a", ChatInput(message="Unknown?", request_id=uuid4())
    )
    assert result["outcome"] == "escalated"
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["result"]["ok"] is True
    assert "TKT-" in result["answer"]


async def test_unknown_tool_cannot_execute_and_loop_is_bounded(db):
    models = FakeModels()
    response = AIMessage(
        content="", tool_calls=[{"name": "unregistered_tool", "args": {}, "id": "call_x"}]
    )
    models.chat = Chat([response, response, response])
    result = await Agent(db, models, EmptyRetriever(), settings()).run(
        "a", ChatInput(message="Unknown?", request_id=uuid4())
    )
    assert result["outcome"] == "escalated"
    assert len(result["tool_results"]) == 3
    assert result["tool_results"][0]["result"]["ok"] is False
    assert result["tool_results"][-1]["result"]["ok"] is True


class KnowledgeRetriever:
    async def search(self, *args, kind="knowledge_units", **kwargs):
        return (
            []
            if kind == "tools"
            else [SimpleNamespace(id=uuid4(), title="Fee", content="Consultation costs INR 500.")]
        )


async def test_faq_one_call_and_no_citations_or_verifier(db):
    models = FakeModels()
    models.chat = Chat([AIMessage(content="Consultation costs INR 500.")])
    result = await Agent(db, models, KnowledgeRetriever(), settings()).run(
        "a", ChatInput(message="What is the fee?", request_id=uuid4())
    )
    assert result["outcome"] == "answered"
    assert "citations" not in result
    import json

    sent = json.loads(models.chat.last_messages[0].content.split("KNOWLEDGE:\n", 1)[1])
    assert result["knowledge_units"] == sent
    assert sent[0]["content"] == "Consultation costs INR 500."
    assert not models.chat.responses


async def test_empty_answer_with_knowledge_escalates(db):
    models = FakeModels()
    models.chat = Chat([AIMessage(content="")])
    result = await Agent(db, models, KnowledgeRetriever(), settings()).run(
        "a", ChatInput(message="What is the fee?", request_id=uuid4())
    )
    assert result["outcome"] == "escalated"
    assert "TKT-" in result["answer"]


async def test_two_rounds_allow_third_final_answer(db):
    models = FakeModels()
    tool_request = AIMessage(content="", tool_calls=[{"name": "unknown", "args": {}, "id": "call"}])
    models.chat = Chat([tool_request, tool_request, AIMessage(content="Fee is INR 500.")])
    result = await Agent(db, models, KnowledgeRetriever(), settings()).run(
        "a", ChatInput(message="Fee?", request_id=uuid4())
    )
    assert result["outcome"] == "answered"
    assert len(result["tool_results"]) == 2
    assert not models.chat.responses
