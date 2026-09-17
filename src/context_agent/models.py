import json
import logging

from google import genai
from google.genai import types
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from .schemas import DomainError
from .usage import UsageCallback, current_usage

log = logging.getLogger(__name__)


def processing_error(exc, message):
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if code in (429, 503):
            log.warning("Gemini request unavailable: provider_status=%s", code)
            return DomainError(
                503,
                "model_unavailable",
                "Gemini is temporarily unavailable or its quota is exhausted. Try again later.",
            )
        current = current.__cause__
    log.warning("Model processing failed: exception_type=%s", type(exc).__name__)
    return DomainError(502, "processing_failed", message)


class Models:
    def __init__(self, settings):
        if not settings.gemini_api_key or not settings.gemini_api_key.get_secret_value():
            raise RuntimeError("Set GEMINI_API_KEY in the environment or .env file.")
        key = settings.gemini_api_key.get_secret_value()
        self.chat = ChatGoogleGenerativeAI(
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
            raise processing_error(exc, "Structured model processing failed.") from exc

    async def embed(self, texts: list[str]):
        if not texts:
            return []
        try:
            return [await self._embed_one(f"title: none | text: {text}") for text in texts]
        except Exception as exc:
            raise processing_error(exc, "Embedding generation failed.") from exc

    async def query(self, text: str):
        try:
            return await self._embed_one(f"task: search result | query: {text}")
        except Exception as exc:
            raise processing_error(exc, "Query embedding failed.") from exc
