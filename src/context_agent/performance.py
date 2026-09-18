"""Opt-in owner-request timings; never collect SQL text, parameters, or credentials."""

import json
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4

from sqlalchemy import event

current_timing = ContextVar("request_performance_timing", default=None)
log = logging.getLogger("uvicorn.error")
KINDS = ("db_lock", "db_query", "db_acquire", "db_finish")


@dataclass
class RequestTiming:
    started: float = field(default_factory=perf_counter)
    spans: list = field(default_factory=list)
    queries: int = 0
    closed: bool = False

    def add(self, kind, started, ended):
        if not self.closed:
            self.spans.append((kind, started, ended))

    def snapshot(self, ended):
        # Partition overlapping spans instead of double-counting connection setup
        # queries, transaction cleanup, or concurrent requests within one operation.
        spans = [(k, max(s, self.started), min(e, ended)) for k, s, e in self.spans]
        points = sorted({self.started, ended, *(p for _, s, e in spans for p in (s, e))})
        totals = dict.fromkeys((*KINDS, "app"), 0.0)
        for left, right in zip(points, points[1:]):
            active = {k for k, s, e in spans if s <= left and e >= right}
            kind = next((k for k in KINDS if k in active), "app")
            totals[kind] += (right - left) * 1000
        return {
            **{k: round(v, 2) for k, v in totals.items()},
            "total": round((ended - self.started) * 1000, 2),
            "query_count": self.queries,
        }


@contextmanager
def measure(kind):
    timing = current_timing.get()
    started = perf_counter() if timing else None
    try:
        yield
    finally:
        if timing:
            timing.add(kind, started, perf_counter())


@asynccontextmanager
async def measure_exit(manager):
    """Measure commit/rollback and connection return, excluding transaction body."""
    started = None
    timing = current_timing.get()
    try:
        async with manager as value:
            try:
                yield value
            finally:
                started = perf_counter()
    finally:
        if timing and started is not None:
            timing.add("db_finish", started, perf_counter())


def instrument_engine(engine):
    def finish(context):
        sample = getattr(context, "_request_timing_sample", None)
        if sample is not None:
            context._request_timing_sample = None
            timing, kind, started = sample
            timing.add(kind, started, perf_counter())

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def before(conn, cursor, statement, parameters, context, executemany):
        timing = current_timing.get()
        if timing is not None and not timing.closed:
            kind = context.execution_options.get("request_timing_kind", "db_query")
            timing.queries += kind == "db_query"
            context._request_timing_sample = (timing, kind, perf_counter())

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def after(conn, cursor, statement, parameters, context, executemany):
        finish(context)

    @event.listens_for(engine.sync_engine, "handle_error")
    def failed(exception_context):
        if exception_context.execution_context is not None:
            finish(exception_context.execution_context)


class PerformanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        requested = dict(scope.get("headers", [])).get(b"x-request-timing") == b"1"
        path = scope.get("path", "")
        if not (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and requested
            and (path == "/auth/me" or path.startswith("/admin/"))
        ):
            return await self.app(scope, receive, send)
        timing = RequestTiming()
        token = current_timing.set(timing)

        async def timed_send(message):
            if message["type"] == "http.response.start":
                result = timing.snapshot(perf_counter())
                timing.closed = True
                # Only an authenticated business owner can receive these metrics.
                if scope.get("state", {}).get("performance_owner_verified"):
                    request_id = uuid4().hex
                    value = (
                        ", ".join(
                            f"{key};dur={result[key]:.2f}" for key in (*KINDS, "app", "total")
                        )
                        + f', db_count;desc="{result["query_count"]}"'
                    )
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (b"server-timing", value.encode()),
                            (b"x-timing-id", request_id.encode()),
                        ],
                    }
                    route = getattr(scope.get("route"), "path", "unmatched")
                    log.info(
                        "request_timing %s",
                        json.dumps(
                            {
                                "id": request_id,
                                "route": route,
                                "status": message["status"],
                                "milliseconds": result,
                            }
                        ),
                    )
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            timing.closed = True
            current_timing.reset(token)
