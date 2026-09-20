import hashlib
import json
import logging

from google import genai
from google.genai import types
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from .schemas import DomainError
from .usage import UsageCallback, current_usage

log = logging.getLogger(__name__)


class GeminiChatModel(ChatGoogleGenerativeAI):
    """Preserve Gemini 3.8's request contract with the pinned LangChain adapter."""

    def _prepare_request(self, messages, **kwargs):
        request = super()._prepare_request(messages, **kwargs)
        if self.model.removeprefix("models/") != "gemini-3.8-flash":
            return request
        config = request["config"]
        for field in ("candidate_count", "temperature", "top_p", "top_k"):
            setattr(config, field, None)
        contents = request["contents"]
        if contents and contents[-1].role == "model":
            raise ValueError("Gemini 3.8 requires a user message or tool response as the final turn.")

        # The adapter retains provider IDs on AIMessage, but drops them when
        # rebuilding FunctionCall/FunctionResponse. Keep IDs and signatures
        # together, including parallel calls and multiple tool rounds.
        calls = []
        responses = []
        for message in messages:
            if isinstance(message, AIMessage) and message.tool_calls:
                calls.extend(message.tool_calls)
                ids = {call["id"] for call in message.tool_calls}
                responses.extend(
                    item for item in messages
                    if isinstance(item, ToolMessage) and item.tool_call_id in ids
                )
        call_parts = [p.function_call for c in contents for p in c.parts or [] if p.function_call]
        result_parts = [
            p.function_response for c in contents for p in c.parts or [] if p.function_response
        ]
        for part, call in zip(call_parts, calls, strict=True):
            part.id = call["id"]
        for part, response in zip(result_parts, responses, strict=True):
            part.id = response.tool_call_id
        return request


def processing_error(exc, message, *, operation="unknown"):
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if code in (401, 403, 429, 503):
            log.warning("Gemini request failed: operation=%s provider_status=%s", operation, code)
            if code == 429:
                return DomainError(
                    503,
                    "model_rate_limited",
                    "Gemini rejected the request because of a rate or quota limit (HTTP 429). "
                    "Check the key's project usage and limits in Google AI Studio.",
                )
            if code in (401, 403):
                return DomainError(
                    503,
                    "model_authorization_failed",
                    "Gemini rejected access. Check the API key and its permissions in Integrations.",
                )
            return DomainError(
                503,
                "model_unavailable",
                "Gemini is temporarily unavailable (HTTP 503). Try again later.",
            )
        current = current.__cause__
    log.warning(
        "Model processing failed: operation=%s exception_type=%s", operation, type(exc).__name__
    )
    return DomainError(502, "processing_failed", message)


class Models:
    def __init__(self, settings):
        if not settings.gemini_api_key or not settings.gemini_api_key.get_secret_value():
            raise RuntimeError("Set GEMINI_API_KEY in the environment or .env file.")
        key = settings.gemini_api_key.get_secret_value()
        self.cache_credential_fingerprint = hashlib.sha256(key.encode()).hexdigest()
        self.chat = GeminiChatModel(
            model=settings.chat_model,
            api_key=key,
            vertexai=False,
            timeout=settings.request_timeout,
            max_retries=2,
            callbacks=[UsageCallback()],
        )
        self.embedding_model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self.embeddings = genai.Client(
            api_key=key,
            vertexai=False,
            http_options=types.HttpOptions(
                timeout=int(settings.request_timeout * 1000),
                retry_options=types.HttpRetryOptions(attempts=3),
            ),
        )

    async def create_context_cache(self, instruction, schemas, expires_at):
        # Use the same schema conversion as ordinary LangChain generations.
        tools = self.chat._format_tools(schemas, None)
        return await self.embeddings.aio.caches.create(
            model=self.chat.model,
            config=types.CreateCachedContentConfig(
                system_instruction=instruction,
                tools=tools or None,
                expire_time=expires_at,
                http_options=types.HttpOptions(
                    timeout=8000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ),
        )

    async def delete_context_cache(self, name):
        await self.embeddings.aio.caches.delete(
            name=name,
            config=types.DeleteCachedContentConfig(
                http_options=types.HttpOptions(
                    timeout=2000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                )
            ),
        )

    async def _embed_one(self, text: str):
        # One input per call: Embedding 2 aggregates multiple inputs into one vector.
        usage = current_usage.get()
        if usage is not None:
            usage.embedding_calls += 1
        try:
            response = await self.embeddings.aio.models.embed_content(
                model=self.embedding_model,
                contents=text,
                config=types.EmbedContentConfig(output_dimensionality=self.dimensions),
            )
        except Exception:
            if usage is not None:
                usage.embedding_complete = False
            raise
        if usage is not None:
            counts = [
                getattr(getattr(item, "statistics", None), "token_count", None)
                for item in (response.embeddings or [])
            ]
            if counts and all(count is not None for count in counts):
                usage.embedding_tokens += sum(counts)
            else:
                usage.embedding_complete = False
        if not response.embeddings or len(response.embeddings) != 1:
            raise ValueError("Expected exactly one embedding")
        vector = response.embeddings[0].values
        if vector is None or len(vector) != self.dimensions:
            raise ValueError("Embedding dimensions do not match the vector index")
        return vector

    async def close(self):
        try:
            await self.chat.aclose()
        finally:
            await self.embeddings.aio.aclose()
            self.embeddings.close()

    async def structured(self, schema, instruction: str, data):
        try:
            result = await self.chat.with_structured_output(schema, method="json_schema").ainvoke(
                [
                    SystemMessage(content=instruction),
                    HumanMessage(content=json.dumps(data, ensure_ascii=False, default=str)),
                ]
            )
            return result if isinstance(result, schema) else schema.model_validate(result)
        except Exception as exc:
            raise processing_error(
                exc, "Structured model processing failed.", operation="structured_generation"
            ) from exc

    async def embed(self, texts: list[str]):
        if not texts:
            return []
        try:
            return [await self._embed_one(f"title: none | text: {text}") for text in texts]
        except Exception as exc:
            raise processing_error(
                exc, "Embedding generation failed.", operation="document_embedding"
            ) from exc

    async def query(self, text: str):
        try:
            return await self._embed_one(f"task: search result | query: {text}")
        except Exception as exc:
            raise processing_error(
                exc, "Query embedding failed.", operation="query_embedding"
            ) from exc
