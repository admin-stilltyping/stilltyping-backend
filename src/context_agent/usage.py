"""Per-request response timing and provider-reported token usage."""

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

from langchain_core.callbacks import BaseCallbackHandler

current_usage = ContextVar("request_token_usage", default=None)


@contextmanager
def track_time(attr):
    """Add wall-clock time spent in the block to the active TokenUsage, if any."""
    usage = current_usage.get()
    started = perf_counter() if usage else None
    try:
        yield
    finally:
        if usage:
            setattr(usage, attr, getattr(usage, attr) + (perf_counter() - started) * 1000)


def response_timing(state):
    started_at = state.get("message_started_at")
    if started_at is None:
        return {}
    return {"response_time_ms": round((perf_counter() - started_at) * 1000, 2)}


@dataclass
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_complete: bool = True
    output_tokens: int = 0
    llm_total: int = 0
    llm_calls: int = 0
    embedding_tokens: int = 0
    embedding_calls: int = 0
    llm_complete: bool = True
    embedding_complete: bool = True
    seen: set = field(default_factory=set)
    # Wall-clock time inside model calls and tool execution, so a slow reply's
    # duration_ms can be split from database time without a second contextvar.
    ai_ms: float = 0.0
    tool_ms: float = 0.0

    def cache_counts(self):
        if not self.cache_complete or not self.llm_complete or self.llm_calls == 0:
            return None, None
        return self.cached_input_tokens, self.input_tokens - self.cached_input_tokens

    def as_dict(self):
        complete = self.llm_complete and self.embedding_complete
        known = self.llm_total + self.embedding_tokens
        return {
            "llm": {
                "calls": self.llm_calls,
                "input_tokens": self.input_tokens,
                "cached_input_tokens": self.cache_counts()[0],
                "uncached_input_tokens": self.cache_counts()[1],
                "output_tokens": self.output_tokens,
                "total_tokens": self.llm_total,
                "complete": self.llm_complete,
            },
            "embeddings": {
                "calls": self.embedding_calls,
                "input_tokens": self.embedding_tokens if self.embedding_complete else None,
                "complete": self.embedding_complete,
            },
            "total_tokens": known if complete else None,
            "reported_total_tokens": known,
            "complete": complete,
        }


class UsageCallback(BaseCallbackHandler):
    run_inline = True

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = current_usage.get()
        if usage is None or run_id in usage.seen:
            return
        usage.seen.add(run_id)
        usage.llm_calls += 1
        for generations in response.generations:
            # Chat requests use one candidate; avoid double-counting provider totals.
            metadata = (
                getattr(generations[0].message, "usage_metadata", None) if generations else None
            )
            if not metadata:
                usage.llm_complete = False
                continue
            cached = (metadata.get("input_token_details") or {}).get("cache_read")
            if isinstance(cached, int) and 0 <= cached <= metadata["input_tokens"]:
                usage.cached_input_tokens += cached
            else:
                usage.cache_complete = False
            usage.input_tokens += metadata["input_tokens"]
            usage.output_tokens += metadata["output_tokens"]
            usage.llm_total += metadata["total_tokens"]

    def on_llm_error(self, error, *, run_id, **kwargs):
        usage = current_usage.get()
        if usage is not None and run_id not in usage.seen:
            usage.seen.add(run_id)
            usage.llm_calls += 1
            usage.llm_complete = False


class UsageMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        if scope["method"] == "POST" and re.fullmatch(
            r"/api/v1/tenants/[^/]+/agent/messages", scope["path"]
        ):
            state["message_started_at"] = perf_counter()
        usage = TokenUsage()
        state["token_usage"] = usage
        token = current_usage.set(usage)
        start = None
        body = bytearray()

        async def send_usage(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    data = json.loads(body)
                    data["usage"] = usage.as_dict()
                    data.update(response_timing(state))
                    output = json.dumps(data, ensure_ascii=False).encode()
                    start["headers"] = [
                        (k, v) for k, v in start["headers"] if k.lower() != b"content-length"
                    ] + [(b"content-length", str(len(output)).encode())]
                    await send(start)
                    await send({"type": "http.response.body", "body": output})
            else:
                await send(message)

        try:
            await self.app(scope, receive, send_usage)
        finally:
            current_usage.reset(token)
