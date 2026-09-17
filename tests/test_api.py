import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from context_agent.api import create_app
from context_agent.schemas import DomainError


class Knowledge:
    def __init__(self):
        self.document_id = uuid4()
        self.exists = False

    async def put(self, tenant, payload):
        created = not self.exists
        self.exists = True
        return {"document_id": self.document_id}, created

    async def add(self, tenant, payload):
        raise DomainError(404, "document_not_found", "This tenant has no document.")


def client():
    return TestClient(
        create_app({"settings": SimpleNamespace(request_timeout=10), "knowledge": Knowledge()})
    )


def test_put_contract_create_and_replace():
    with client() as api:
        payload = {"title": "Clinic", "summary": "Fee is 1000"}
        created = api.put("/api/v1/tenants/a/document", json=payload)
        replaced = api.put("/api/v1/tenants/a/document", json=payload)
        assert created.status_code == 201 and replaced.status_code == 200
        assert created.json() == replaced.json()
        assert set(created.json()) == {"document_id", "usage"}
        assert created.json()["usage"]["total_tokens"] == 0


def test_validation_and_error_contract():
    with client() as api:
        response = api.put("/api/v1/tenants/a/document", json={"title": "x", "summary": " "})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"
        response = api.post("/api/v1/tenants/a/document/knowledge-units", json={"content": "hello"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "document_not_found"


def test_reserved_scope_and_oversized_body_rejected():
    with client() as api:
        response = api.put(
            "/api/v1/tenants/general/document", json={"title": "x", "summary": "fact"}
        )
        assert response.status_code == 400
        response = api.put("/api/v1/tenants/a/document", content=b"x" * 300001)
        assert response.status_code == 400


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (None, 200),
        (DomainError(502, "processing_failed", "Query embedding failed."), 502),
        (RuntimeError("unexpected provider failure"), 502),
    ],
)
def test_message_response_time_includes_processing_on_success_and_errors(failure, status):
    knowledge = [{"id": str(uuid4()), "title": "Greeting", "content": "Hello!"}]

    class Agent:
        async def run(self, tenant, request):
            await asyncio.sleep(0.02)
            if failure is not None:
                raise failure
            return {
                "request_id": request.request_id,
                "outcome": "answered",
                "answer": "Hello!",
                "knowledge_units": knowledge,
            }

    app = create_app({"settings": SimpleNamespace(request_timeout=10), "agent": Agent()})
    with TestClient(app, raise_server_exceptions=False) as api:
        response = api.post(
            "/api/v1/tenants/a/agent/messages",
            json={"request_id": str(uuid4()), "message": "Hello"},
        )
        assert response.status_code == status
        data = response.json()
        assert isinstance(data["response_time_ms"], (int, float))
        assert data["response_time_ms"] >= 20
        assert "usage" in data
        assert int(response.headers["content-length"]) == len(response.content)
        if failure is None:
            assert data["answer"] == "Hello!"
            assert data["knowledge_units"] == knowledge
        else:
            assert data["error"]["code"] == "processing_failed"
        schema = api.get("/openapi.json").json()["components"]["schemas"]["ChatOutput"]
        assert "response_time_ms" in schema["properties"]


def test_message_validation_errors_have_response_time():
    with client() as api:
        response = api.post("/api/v1/tenants/a/agent/messages", json={"message": "Hello"})
        assert response.status_code == 400
        assert response.json()["response_time_ms"] >= 0
        assert "response_time_ms" not in api.get("/health/live").json()


def test_message_timeouts_have_response_time():
    class Agent:
        async def run(self, tenant, request):
            await asyncio.sleep(0.03)
            return {"request_id": request.request_id, "outcome": "answered", "answer": "Hello!"}

    app = create_app({"settings": SimpleNamespace(request_timeout=0.01), "agent": Agent()})
    with TestClient(app) as api:
        response = api.post(
            "/api/v1/tenants/a/agent/messages",
            json={"request_id": str(uuid4()), "message": "Hello"},
        )
        assert response.status_code == 502
        assert response.json()["response_time_ms"] >= 10
        assert response.json()["error"]["code"] == "processing_failed"
