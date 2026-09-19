from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from context_agent.config import Settings
from context_agent.models import Models
from context_agent.schemas import DomainError


class Answer(BaseModel):
    answer: str


async def test_gemini_configuration_and_embedding_contract(monkeypatch, tmp_path):
    # Key comes from .env, without depending on SDK environment auto-discovery.
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=test-placeholder\n")
    settings = Settings(_env_file=env)
    models = Models(settings)
    try:
        assert models.chat.model == "gemini-3.5-flash"
        models.chat.with_structured_output(Answer, method="json_schema")
        models.chat.bind_tools(
            [
                {
                    "name": "lookup",
                    "description": "Look up a fact",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ]
        )
        sdk = AsyncMock(
            return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * 3072)])
        )
        monkeypatch.setattr(models.embeddings.aio.models, "embed_content", sdk)
        result = await models.embed(["first document", "second document"])
        query = await models.query("question")
        assert len(result) == 2 and len(query) == 3072
        calls = [call.kwargs for call in sdk.await_args_list]
        assert [call["contents"] for call in calls] == [
            "title: none | text: first document",
            "title: none | text: second document",
            "task: search result | query: question",
        ]
        assert all(call["model"] == "gemini-embedding-2" for call in calls)
        assert all(call["config"].output_dimensionality == 3072 for call in calls)
        assert await models.embed([]) == []
        assert sdk.await_count == 3
        sdk.return_value = SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1])])
        with pytest.raises(DomainError):
            await models.embed(["bad dimensions"])
        sdk.side_effect = RuntimeError("provider unavailable")
        with pytest.raises(DomainError):
            await models.query("question")
    finally:
        await models.close()


def test_missing_key_has_actionable_error():
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        Models(Settings(_env_file=None, gemini_api_key=""))


@pytest.mark.parametrize("code, expected", [
    (429, "model_rate_limited"),
    (503, "model_unavailable"),
    (401, "model_authorization_failed"),
    (403, "model_authorization_failed"),
])
def test_provider_unavailability_is_not_hidden(code, expected, caplog):
    from context_agent.models import processing_error

    cause = RuntimeError("secret-key-in-provider-url")
    cause.code = code
    wrapper = RuntimeError("adapter failure")
    wrapper.__cause__ = cause
    error = processing_error(wrapper, "Processing failed", operation="query_embedding")
    assert error.status == 503
    assert error.code == expected
    assert "operation=query_embedding" in caplog.text
    assert f"provider_status={code}" in caplog.text
    assert "secret-key" not in caplog.text
    assert "secret-key" not in str(error)


async def test_query_embedding_preserves_provider_limit_and_operation(monkeypatch, caplog):
    from google.genai.errors import ClientError

    models = Models(Settings(_env_file=None, gemini_api_key="test-placeholder"))
    monkeypatch.setattr(
        models.embeddings.aio.models,
        "embed_content",
        AsyncMock(side_effect=ClientError(429, {"error": {"message": "secret-api-key"}})),
    )
    try:
        with pytest.raises(DomainError) as failure:
            await models.query("private-customer-message")
        assert failure.value.code == "model_rate_limited"
        assert "operation=query_embedding provider_status=429" in caplog.text
        assert "secret-api-key" not in caplog.text
        assert "private-customer-message" not in caplog.text
    finally:
        await models.close()


def test_other_processing_errors_remain_502():
    from context_agent.models import processing_error

    assert processing_error(ValueError("bad output"), "Processing failed").status == 502
