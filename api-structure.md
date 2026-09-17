# Knowledge API structure

Each tenant has one document. Tenant creation is a separate flow. The tenant ID is supplied in the URL.

All requests use `Content-Type: application/json`. Tenant authentication and authorization are outside this document's scope; the tenant contract below does not include 401 or 403 responses. Super-admin authentication is separately implemented at `/auth/super-admin/login` and `/auth/super-admin/me`; see [its setup and API guide](src/super_admin/README.md). Business registry, subdomain provisioning and protected owner profiles are documented in the [business API guide](src/super_admin/businesses/README.md).

## Endpoint overview

| Action | Method | URL |
|---|---|---|
| Create or replace the whole document | PUT | /api/v1/tenants/{tenant_id}/document |
| Add knowledge | POST | /api/v1/tenants/{tenant_id}/document/knowledge-units |
| Update existing knowledge | PATCH | /api/v1/tenants/{tenant_id}/document/knowledge-units |

## 1. Create or replace the whole document

**Method:** `PUT`

**URL:** `/api/v1/tenants/{tenant_id}/document`

### Request

```json
{
  "title": "Clinic Information",
  "summary": "Consultation costs ₹1000. Appointments last 30 minutes."
}
```

| Field | Type | Required |
|---|---|---|
| title | Non-empty string | Yes |
| summary | Non-empty string | Yes |

### Behavior

- If no document exists, create the document and its self-contained knowledge units.
- If a document exists, replace its title and all knowledge units, keeping the same document_id.
- Information omitted from the replacement summary is removed.
- Generate and index the replacement before making it active. A processing failure leaves the previous document usable.
- Clean up the previous knowledge units and their vectors after switching to the replacement.

### Response

`201 Created` for creation; `200 OK` for replacement.

```json
{
  "document_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

There is no document_already_exists error: existing documents are replaced.

## 2. Add knowledge

**Method:** `POST`

**URL:** `/api/v1/tenants/{tenant_id}/document/knowledge-units`

### Request

```json
{
  "content": "We offer online consultations for ₹800. They are available Monday to Friday."
}
```

| Field | Type | Required |
|---|---|---|
| content | Non-empty string | Yes |

### Behavior

- Require an existing document for the tenant.
- Convert input into one or more self-contained knowledge units.
- Save the new units and generate their embeddings.
- Preserve existing knowledge units; this endpoint does not update them.

### Response

`201 Created`

```json
{
  "created_unit_ids": [
    "660e8400-e29b-41d4-a716-446655440003"
  ]
}
```

## 3. Update existing knowledge

**Method:** `PATCH`

**URL:** `/api/v1/tenants/{tenant_id}/document/knowledge-units`

### Request

```json
{
  "change": "Change the online consultation fee to ₹1000."
}
```

| Field | Type | Required |
|---|---|---|
| change | Non-empty string | Yes |

### Behavior

- Require an existing document for the tenant.
- Retrieve matching knowledge units within that tenant's document.
- Update affected units while preserving unrelated information.
- Keep existing knowledge-unit IDs and regenerate only affected embeddings.
- Do not add knowledge when no match exists; return knowledge_not_found.
- If clarification is required, make no changes. The caller can submit a clearer change string to the same endpoint.

### Response

`200 OK`

```json
{
  "updated_unit_ids": [
    "660e8400-e29b-41d4-a716-446655440003"
  ]
}
```

If the knowledge already matches the requested change, return `200 OK`:

```json
{
  "updated_unit_ids": []
}
```

### No matching knowledge

`404 Not Found`

```json
{
  "error": {
    "code": "knowledge_not_found",
    "message": "No existing knowledge matches the requested change."
  }
}
```

### Ambiguous change

`409 Conflict`

```json
{
  "error": {
    "code": "clarification_required",
    "message": "Should the standard or premium consultation fee change?",
    "candidates": [
      {
        "id": "660e8400-e29b-41d4-a716-446655440001",
        "title": "Standard Consultation Fee"
      },
      {
        "id": "660e8400-e29b-41d4-a716-446655440002",
        "title": "Premium Consultation Fee"
      }
    ]
  }
}
```

## Common errors

All errors use an error object with code and message. A clarification response can additionally include candidates.

```json
{
  "error": {
    "code": "validation_error",
    "message": "The content field must not be empty."
  }
}
```

| HTTP status | Error code | Applies to / meaning |
|---|---|---|
| 400 | validation_error | All endpoints; missing, empty, or invalid input |
| 404 | document_not_found | Add and update; tenant has no document |
| 404 | knowledge_not_found | Update; no matching existing knowledge |
| 409 | clarification_required | Input cannot be interpreted unambiguously; no changes made |
| 409 | concurrent_update | Conflicting changes during processing |
| 502 | processing_failed | LLM or embedding service failure |
| 503 | storage_unavailable | PostgreSQL or Qdrant unavailable |

## Processing rules

- APIs are synchronous initially. Return success after saving and indexing complete.
- Scope all database queries and vector searches to the tenant ID supplied in the URL. This data filtering remains necessary even while authentication is outside scope.
- Enforce UNIQUE (tenant_id) on the documents table to guarantee one document per tenant.
- Track incomplete indexing work using embedding_status. The initial implementation provides the context-agent reconcile command for retries; an automatic background worker is not yet implemented. Failed or pending records are excluded from retrieval.
- Whole-document replacement must switch to the prepared replacement as one operation. Clean up previous records and vectors afterward.
- PostgreSQL and Qdrant are separate systems; the implementation must explicitly coordinate staging, switching, and retries rather than assume a shared transaction.

## Scope

This contract covers document creation/replacement, knowledge addition, and knowledge updates. It does not define tenant creation, authentication, authorization, tool-management APIs, or question-answering APIs.

## Per-request token usage

All API responses now also include a top-level `usage` object (including the document-ID response and errors). It contains `llm` (calls, input_tokens, output_tokens, total_tokens, complete), `embeddings` (calls, input_tokens, complete), `total_tokens`, `reported_total_tokens`, and `complete`. Missing provider counts produce null embedding/overall totals and complete=false; reported_total_tokens is only the known subtotal. This is token usage, not monetary cost.

## Optimized chat response

Chat responses contain request_id, outcome, answer, tool_results, knowledge_units, response_time_ms and usage. The citations field is removed. Chat uses no LLM reranking or separate verification; ordinary FAQs require one logical chat call, with a maximum of MAX_TOOL_ROUNDS + 1. Empty answers escalate. Relevance filtering uses provisional cosine >= 0.60 and up to four knowledge units/five retrieved tools plus support fallback.

Chat responses also include `knowledge_units`: the exact ordered knowledge context supplied to the answer LLM, with `id`, `title`, and `content`. This is populated from graph state by code, not generated by the model, and adds no LLM output tokens. It is an empty list when no knowledge was supplied and remains present for escalation outcomes. These are supplied context, not verified citations.

`response_time_ms` measures server processing with a monotonic clock, in milliseconds rounded to two
decimal places. It includes request parsing, embeddings, retrieval, model/tool calls and preparing
the response, and is also returned for message API errors. It excludes final response transmission
and client network latency. For example, `"response_time_ms": 1543.27` means about 1.54 seconds.
