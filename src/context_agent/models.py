import hashlib
import json
import logging

from google import genai
from google.genai import types
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

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
            raise ValueError(
                "Gemini 3.8 requires a user message or tool response as the final turn."
            )

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
                    item
                    for item in messages
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
        code = getattr(current, "status_code", None) or getattr(current, "code", None)
        if code in (401, 403, 429, 503):
            log.warning("Model request failed: operation=%s provider_status=%s", operation, code)
            if code == 429:
                return DomainError(
                    503,
                    "model_rate_limited",
                    "The AI provider rejected the request because of a rate or quota limit (HTTP 429). "
                    "Check the provider account usage and limits.",
                )
            if code in (401, 403):
                return DomainError(
                    503,
                    "model_authorization_failed",
                    "The AI provider rejected access. Check the API key and its permissions in Integrations.",
                )
            return DomainError(
                503,
                "model_unavailable",
                "The AI provider is temporarily unavailable (HTTP 503). Try again later.",
            )
        current = current.__cause__
    log.warning(
        "Model processing failed: operation=%s exception_type=%s", operation, type(exc).__name__
    )
    return DomainError(502, "processing_failed", message)


class Models:
    def __init__(self, settings):
        llm_secret = settings.llm_api_key or settings.gemini_api_key
        embedding_secret = settings.embedding_api_key or settings.gemini_api_key
        if not llm_secret or not embedding_secret:
            raise RuntimeError(
                "Set GEMINI_API_KEY or configure both LLM and embedding credentials."
            )
        key = llm_secret.get_secret_value()
        embedding_key = embedding_secret.get_secret_value()
        self.embedding_provider = settings.embedding_provider
        self.llm_provider = settings.llm_provider
        self.embedding_model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        if self.llm_provider == "gemini":
            self.cache_credential_fingerprint = hashlib.sha256(key.encode()).hexdigest()
            self.chat = GeminiChatModel(
                model=settings.chat_model,
                api_key=key,
                vertexai=False,
                timeout=settings.request_timeout,
                max_retries=2,
                callbacks=[UsageCallback()],
            )
        else:
            self.chat = ChatOpenAI(
                model=settings.chat_model,
                api_key=key,
                base_url=(
                    "https://api.deepinfra.com/v1/openai"
                    if self.llm_provider == "deepinfra"
                    else "https://api.deepseek.com"
                ),
                timeout=settings.request_timeout,
                max_retries=2,
                callbacks=[UsageCallback()],
                extra_body={"thinking": {"type": "disabled"}}
                if self.llm_provider == "deepseek"
                else None,
            )
        if self.embedding_provider == "gemini":
            self.embeddings = genai.Client(
                api_key=embedding_key,
                vertexai=False,
                http_options=types.HttpOptions(
                    timeout=int(settings.request_timeout * 1000),
                    retry_options=types.HttpRetryOptions(attempts=3),
                ),
            )
        else:
            self.embeddings = AsyncOpenAI(
                api_key=embedding_key,
                base_url="https://api.deepinfra.com/v1/openai",
                timeout=settings.request_timeout,
                max_retries=2,
            )
        self.cache_client = None
        if self.llm_provider == "gemini":
            self.cache_client = (
                self.embeddings
                if self.embedding_provider == "gemini" and key == embedding_key
                else genai.Client(api_key=key, vertexai=False)
            )

    async def create_context_cache(self, instruction, schemas, expires_at):
        # Use the same schema conversion as ordinary LangChain generations.
        tools = self.chat._format_tools(schemas, None)
        return await self.cache_client.aio.caches.create(
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
        await self.cache_client.aio.caches.delete(
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
            if self.embedding_provider == "deepinfra":
                response = await self.embeddings.embeddings.create(
                    model=self.embedding_model,
                    input=[text],
                    dimensions=self.dimensions,
                    encoding_format="float",
                )
                if usage is not None:
                    count = getattr(response.usage, "prompt_tokens", None)
                    if count is None:
                        usage.embedding_complete = False
                    else:
                        usage.embedding_tokens += count
                vector = response.data[0].embedding
                if len(vector) != self.dimensions:
                    raise ValueError("Embedding dimensions do not match the vector index")
                return vector
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
        if self.llm_provider == "gemini":
            await self.chat.aclose()
        else:
            await self.chat.root_async_client.close()
            self.chat.root_client.close()
        if self.embedding_provider == "gemini":
            await self.embeddings.aio.aclose()
            self.embeddings.close()
        else:
            await self.embeddings.close()
        if self.cache_client is not None and self.cache_client is not self.embeddings:
            await self.cache_client.aio.aclose()
            self.cache_client.close()

    async def structured(self, schema, instruction: str, data):
        try:
            result = await self.chat.with_structured_output(
                schema,
                method="json_schema" if self.llm_provider == "gemini" else "function_calling",
            ).ainvoke(
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
            return [
                await self._embed_one(
                    f"title: none | text: {text}" if self.embedding_provider == "gemini" else text
                )
                for text in texts
            ]
        except Exception as exc:
            raise processing_error(
                exc, "Embedding generation failed.", operation="document_embedding"
            ) from exc

    async def query(self, text: str):
        try:
            query = (
                f"task: search result | query: {text}"
                if self.embedding_provider == "gemini"
                else f"Instruct: Retrieve relevant business knowledge or tools for the query.\nQuery: {text}"
            )
            return await self._embed_one(query)
        except Exception as exc:
            raise processing_error(
                exc, "Query embedding failed.", operation="query_embedding"
            ) from exc
