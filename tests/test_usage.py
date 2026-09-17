import asyncio
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from context_agent.usage import TokenUsage, UsageCallback, current_usage


def result(input_tokens, output_tokens):
    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="answer",
                        usage_metadata={
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": input_tokens + output_tokens,
                        },
                    )
                )
            ]
        ]
    )


async def test_concurrent_requests_are_isolated_and_calls_accumulate():
    callback = UsageCallback()

    async def request(tokens):
        usage = TokenUsage()
        token = current_usage.set(usage)
        try:
            run = uuid4()
            callback.on_llm_end(result(tokens, 2), run_id=run)
            await asyncio.sleep(0)
            callback.on_llm_end(result(tokens, 2), run_id=run)  # Duplicate callback.
            callback.on_llm_end(result(3, 4), run_id=uuid4())
            return usage.as_dict()
        finally:
            current_usage.reset(token)

    a, b = await asyncio.gather(request(10), request(100))
    assert a["total_tokens"] == 19 and b["total_tokens"] == 109
    assert a["llm"]["calls"] == b["llm"]["calls"] == 2
    assert current_usage.get() is None


def test_failed_or_unreported_calls_do_not_claim_exact_totals():
    usage = TokenUsage()
    token = current_usage.set(usage)
    try:
        callback = UsageCallback()
        callback.on_llm_end(result(10, 5), run_id=uuid4())
        callback.on_llm_error(RuntimeError(), run_id=uuid4())
        usage.embedding_calls = 1
        usage.embedding_complete = False
        summary = usage.as_dict()
        assert summary["total_tokens"] is None
        assert summary["reported_total_tokens"] == 15
        assert summary["embeddings"]["input_tokens"] is None
        assert not summary["complete"]
    finally:
        current_usage.reset(token)


async def test_embedding_without_metadata_is_unknown(monkeypatch):
    from unittest.mock import AsyncMock

    from context_agent.config import Settings
    from context_agent.models import Models

    models = Models(Settings(_env_file=None, gemini_api_key="test-placeholder"))
    monkeypatch.setattr(
        models.embeddings.aio.models,
        "embed_content",
        AsyncMock(return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * 3072)])),
    )
    usage = TokenUsage()
    token = current_usage.set(usage)
    try:
        await models.query("question")
        assert usage.as_dict()["embeddings"] == {
            "calls": 1,
            "input_tokens": None,
            "complete": False,
        }
    finally:
        current_usage.reset(token)
        await models.close()
