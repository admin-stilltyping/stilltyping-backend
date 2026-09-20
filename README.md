# Context Agent

FastAPI + LangGraph agent using matching knowledge units and code-defined tools. PostgreSQL is authoritative, Qdrant supplies semantic retrieval, PostgreSQL full-text search adds lexical candidates, and RRF combines the candidate rankings. Stored-vector cosine similarity filters candidates before the answer model. There is no LLM reranker or separate verifier.

This is an initial implementation with deterministic tests, not a claim of measured production accuracy. Real customer evaluation, provider configuration and live service verification are required before deployment.

## Open in PyCharm

Open this folder as the project. Project display name: **Context Agent** (`context-agent`). Select `.venv/bin/python` as its existing Python interpreter. The Python packages are `context_agent`, `super_admin`, `dashboard`, `knowledge_base`, `custom_fields`, `products`, and `modules`.

## Super-admin and business portals

Super-admin authentication lives in `src/super_admin/`. It provides
`POST /auth/super-admin/login` and protected `GET /auth/super-admin/me`, matching
the admin frontend's login request. Accounts are provisioned through the CLI;
there is no default username/password or public signup.

See the [super-admin setup guide](src/super_admin/README.md) for the server-only
signing secret, migration, account creation, and password reset commands.
Business creation, generated subdomain slugs, permanent `_id` values, owner logins,
and protected saved profiles are implemented in `src/super_admin/businesses/`.
See the [business API and local portal guide](src/super_admin/businesses/README.md).
The [demo dashboard](src/dashboard/README.md) provides 16 reusable widget types,
10 saved widgets per business, and use-case-specific synthetic values. Its
protected routes live in `src/dashboard/`; the business portal opens `/dashboard`
after login.
The [Knowledge Base editor](src/knowledge_base/README.md) reads and saves business
knowledge through authenticated routes, using the existing extraction and indexing flow.
The [product catalog](src/products/README.md) stores business-owned products and
validated JSONB attributes. [Custom field definitions](src/custom_fields/README.md)
are independently configured per business and entity type. Product creation,
editing, search, pagination and archiving are connected to the business portal.
[Business module access](src/modules/README.md) provides saved super-admin switches,
paired-module dependencies, portal navigation and direct-route guards, and access
checks on existing product, custom field and support APIs. Support management
requires the owning business-admin token. Authorization for other existing tenant
`/api/v1` routes remains separate work.
The [CRM and transaction flow](src/crm/README.md) captures leads from incoming
enquiries and creates customers only when orders or appointments are saved.
Services, orders, appointments, minimal customer identities and enquiry history
are persisted per business and connected to the portal.

## Full local Docker setup

Requires a running Docker engine and Docker Compose. Python/uv do not need to be installed on your host.

1. Copy `.env.example` to `.env` if you do not already have one, and set `GEMINI_API_KEY`.
2. Run:

```bash
docker compose up -d
```

Compose builds the agent image automatically on first use and starts PostgreSQL, Qdrant and the API. The agent waits for storage, applies migrations, initializes vector collections, repairs missing or pending knowledge vectors and syncs code-defined tools, then serves requests. Initialization uses Gemini embedding quota and must succeed before the API starts. Unchanged embeddings are reused on restart. This automatic migration setup is intended for one local agent instance.

- API and interactive docs: http://localhost:8000/docs
- Readiness: http://localhost:8000/health/ready
- Qdrant dashboard: http://localhost:6333/dashboard

```bash
docker compose ps
docker compose logs -f agent
# Wait until the API is healthy (initial embedding may take time):
docker compose up -d --wait --wait-timeout 300
# Rebuild after source/dependency changes:
docker compose up -d --build
# Stop containers, preserving database volumes:
docker compose down
```

Do not add `-v` to `down` unless you intend to delete stored data. Container connections use `postgres:5432` and `qdrant:6333`; Compose overrides the localhost URLs in `.env`. Secrets enter only at container runtime; `.env` and Git history are excluded from the image. FastAPI is exposed only on localhost, so Nginx is unnecessary for this setup. TLS/public hosting is a separate deployment concern. A support service running on your host can be reached using `host.docker.internal` where supported by your Docker runtime.

Maintenance commands can run inside the agent container:

```bash
docker compose exec agent context-agent sync-tools
docker compose exec agent context-agent reconcile --tenant YOUR_TENANT_ID
```

## Optional: run Python outside Docker

Requires Python 3.12+ and uv. Start just storage using `docker compose up -d postgres qdrant`, then run `uv sync --frozen`, `uv run alembic upgrade head`, `uv run context-agent init-index`, `uv run context-agent sync-tools`, and `uv run uvicorn context_agent.api:app --host 127.0.0.1 --port 8000`. Keep localhost storage URLs in `.env` for this mode.

Set `GEMINI_API_KEY` in your local `.env` (which Git ignores). Gemini is the only configured provider: `gemini-3.8-flash` handles agent responses, extraction and updates; `gemini-embedding-2` generates 3072-dimensional knowledge, tool and query vectors. No OpenAI or Anthropic key is required. The chat adapter supports tool calling and native structured output. Embedding requests use Google's search/document task prefixes, one input per request to avoid aggregation. Requests have bounded timeouts and retries.

Google lists free-tier pricing for both defaults, subject to project quotas; a paid project's usage follows its billing tier. Model quality still requires evaluation on your customer data.

The new `INDEX_PREFIX=agent_gemini_embedding_2_3072` isolates Gemini vectors from the old OpenAI space. If existing data is present, stop serving traffic during migration, update `.env`, then run:

```bash
uv run context-agent init-index
uv run context-agent sync-tools --reembed
uv run context-agent reconcile --tenant YOUR_TENANT_ID
```

Repeat reconcile for every tenant before restarting the API. Existing PostgreSQL knowledge is preserved. Any later change to embedding model, dimensions or task formatting requires a fresh prefix and rebuilding both indexes. The embedding adapter's task formatting targets Embedding 2; other model families require adapter changes.

## Knowledge APIs

See api-structure.md for the agreed request/response contracts:

- PUT /api/v1/tenants/{tenant_id}/document: create or replace; stable document ID.
- POST /api/v1/tenants/{tenant_id}/document/knowledge-units: add only.
- PATCH /api/v1/tenants/{tenant_id}/document/knowledge-units: update only, with clarification on ambiguity.

The schema contains documents, knowledge_units and tools. It has no per-record version, section_id, lifecycle status or tenant_tools table. The documents table enforces one document per tenant. Authentication is intentionally outside the requested scope; tenant filtering is enforced in retrieval and canonical database reads.

## Agent endpoint

POST /api/v1/tenants/{tenant_id}/agent/messages

```json
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "What is the consultation fee?"
}
```

Response fields:

- request_id: caller-provided ID.
- outcome: answered, escalated, or escalation_failed.
- answer: grounded answer or deterministic escalation message.
- tool_results: tool name, call ID and result.
- response_time_ms: server processing time in milliseconds, rounded to two decimal places. Includes
  request parsing, embeddings, retrieval, model/tool calls and preparing the response; excludes final
  response transmission and client network latency. Included on message API errors too.

This first endpoint handles independent requests. It does not yet store conversation history or implement durable graph checkpoints. request_id supports support-webhook idempotency, not an API response cache. Do not treat retried arbitrary custom tool calls as exactly-once execution.

## Code-defined tools

Add ToolDefinition objects to REGISTRY in src/context_agent/tools.py, with explicit Python handlers. Definitions include tenant_id, name, description, parameters_schema and handler_key. Run:

```bash
uv run context-agent sync-tools
```

The script preserves deterministic tool IDs, updates PostgreSQL, and embeds new or changed discovery text. Changed implementation code does not trigger embedding. A database definition that no longer matches the code registry cannot execute. Removed definitions are left in storage for explicit cleanup but excluded from execution. Shared tools use tenant_id=general; customer tools use the actual tenant ID. The support fallback remains bound independently of semantic retrieval when Support Tickets is enabled.

## Support tickets

When the agent cannot ground an answer it escalates by creating a durable ticket in the `support_tickets` table (idempotent on tenant_id + request_id, so a retried request never opens a duplicate). The reply then reports the human-readable reference, e.g. "Support ticket TKT-02E1FA was created." No external service is required. Escalation reports `escalation_failed` if ticket creation fails, including when Support Tickets is disabled for a registered business.

Staff read and resolve tickets with the owning business-admin JWT and Support
Tickets enabled through tenant-scoped endpoints:

- GET /api/v1/tenants/{tenant_id}/support-tickets?status=open
- GET /api/v1/tenants/{tenant_id}/support-tickets/{ticket_ref}
- PATCH /api/v1/tenants/{tenant_id}/support-tickets/{ticket_ref} — set status (open|resolved) and/or notes.

Optionally set SUPPORT_WEBHOOK_URL and SUPPORT_WEBHOOK_TOKEN to also notify an external system best-effort (payload: tenant_id, request_id, ticket_ref, question, reason, with an Idempotency-Key header). A webhook failure is logged and never blocks ticket creation. No URL or credentials come from LLM arguments. Custom tool handlers return JSON objects and should use ok=false for failed operations.

## Consistency and recovery

PostgreSQL advisory locks serialize tenant knowledge operations across workers. PUT stages new vectors with new unit IDs, then switches PostgreSQL records in one transaction. Reads hydrate only canonical ready records. Orphan vectors cannot expose deleted content; cleanup failure can nevertheless reduce retrieval recall until reconciliation.

PATCH commits changed content as pending before replacing its stable-ID vector, then rechecks its content hash under the tenant lock before marking ready. A failure after the first commit leaves changed knowledge pending and unavailable until repair. The update is not rolled back to the previous text. This avoids falsely marking mismatched content and vectors ready without adding version columns.

Run after failed indexing or to rebuild one tenant:

```bash
uv run context-agent reconcile --tenant customer_123
```

Reconciliation rebuilds all that tenant's knowledge vectors, marks records ready, and deletes orphan points while holding the same tenant lock. Schedule this operationally if automatic background retries are required; the current implementation exposes an explicit repair command rather than a durable worker. Sync-tools similarly retries pending tool indexing.

PUT retries replace content again. POST adds new records on each successful call. If a response is lost, POST retry may duplicate knowledge. Durable API idempotency and a background job/outbox system remain future operational work; no extra tables were added silently.

## Validation

```bash
uv run ruff check src tests migrations
uv run pytest -q
uv run alembic upgrade head --sql
```

Tests use a temporary SQLite database and fake model responses, plus the actual Qdrant local engine. They test contracts, failure behavior, tenant scope, tool registry synchronization, and LangGraph control flow. They do not measure live LLM accuracy or prove PostgreSQL concurrency behavior.

Production evaluation should measure retrieval recall, factual support, tool choice, incorrect escalation, missed escalation, and latency using representative customer questions. Include ambiguous updates, conflicting policies, exact identifiers, multilingual input, tool failures, and instructions embedded in retrieved data. The answer model must use supplied evidence or request support. Code rejects empty answers or answers with no retrieved knowledge/successful tool evidence, but cannot verify individual factual claims.

## References

- https://docs.langchain.com/oss/python/langgraph/agentic-rag
- https://docs.langchain.com/oss/python/langchain/models
- https://qdrant.tech/documentation/search/hybrid-queries/
- https://ai.google.dev/gemini-api/docs/embeddings
- https://ai.google.dev/gemini-api/docs/pricing

## Call the APIs with a script

Run these from the project folder on your host with Python 3. No additional packages or Gemini key are needed by this client; the running agent handles Gemini credentials.

```bash
python3 scripts/call_api.py health
python3 scripts/call_api.py put-document --tenant demo --title "Customer discussion" --summary "Our store opens at 9 AM and closes at 6 PM."
python3 scripts/call_api.py add-knowledge --tenant demo --content "We are closed on Sundays."
python3 scripts/call_api.py update-knowledge --tenant demo --change "Change the closing time from 6 PM to 7 PM."
python3 scripts/call_api.py chat --tenant demo --message "What time do you close?"
```

`put-document` replaces the complete existing document and its knowledge. For long text, replace `--summary`, `--content`, `--change` or `--message` with `--file path/to/input.txt`. Files must contain plain UTF-8 text, not a JSON payload.

```bash
python3 scripts/call_api.py --base-url http://localhost:8000 --timeout 240 chat --tenant demo --message "Are you open on Sunday?"
python3 scripts/call_api.py put-document --tenant demo --title "Customer discussion" --file discussion.txt
```

Global options go before the command. Chat generates a request UUID; optionally pass `--request-id UUID`. The client prints response JSON to stdout and HTTP status/request information to stderr, and exits nonzero for errors. It does not retry requests automatically. A repeated request ID is not a guarantee of API-wide deduplication.

## PostgreSQL browser UI (pgAdmin)

The Compose stack includes pgAdmin at http://localhost:5050. Start it with the full stack or run `docker compose up -d pgadmin`.

- Login email: `admin@example.com`
- Login password: `agent-local-admin`
- Expand **Servers → Context Agent PostgreSQL** and enter database password `agent` when prompted.
- Browse **Databases → agent → Schemas → public → Tables**. Right-click a table and choose **View/Edit Data → All Rows**, or open **Query Tool** to run SQL.

These are local-development defaults; the UI is bound only to localhost. Override `PGADMIN_DEFAULT_EMAIL` and `PGADMIN_DEFAULT_PASSWORD` in `.env` before its first launch if desired. Login initialization happens only on first creation of the `pgadmin_data` volume; later password changes should be made in pgAdmin. Saved UI settings persist in this volume. Your Gemini key is not passed to pgAdmin.

## Token usage in API responses

Every `/api/` JSON response includes `usage`, including document writes and errors. LLM counts aggregate extraction, updates and agent/tool-selection rounds within that request. Concurrent requests are counted independently. `usage.llm` contains input/output/total tokens and call count. Output tokens follow the adapter's provider usage metadata, including reasoning where reported.

`usage.embeddings.input_tokens` is null when Gemini does not report embedding counts (the Developer API commonly omits them). In that case `usage.total_tokens` is also null, `usage.complete` is false, and `usage.reported_total_tokens` contains only known counts. Do not interpret this subtotal as the full bill. These are provider-reported counts, not a price estimate or billing audit; internal provider retries with no returned usage cannot be measured. Startup tool syncing is outside API-request totals. Health endpoints do not include usage.

## Retrieval and token limits

Each search collects up to 30 dense and 30 lexical candidates. RRF merges their union. Every canonical, tenant-scoped candidate is scored using its stored vector and the one shared query vector, including lexical-only candidates. Missing vectors and cosine scores below `RELEVANCE_THRESHOLD=0.60` are excluded; the remaining RRF order is preserved. This cutoff is provisional, not a probability or a validated accuracy guarantee. It applies to knowledge update candidate discovery too.

`KNOWLEDGE_LIMIT=4` and `TOOL_LIMIT=5` are maxima, never quotas. The code-defined support fallback remains available separately. `MAX_TOOL_ROUNDS=2` allows at most three logical chat calls (tool rounds + one); a round may contain several tool calls. Provider retries are outside this logical limit. Ordinary FAQs use one chat call. No citations are generated or returned. Retrieved IDs are logged as supplied context, not evidence of factual support. Empty answers escalate in code; factual grounding otherwise relies on the answer model's instructions. Tool execution confirmation and tenant/registry checks remain enforced.

The initial live Sunday-hours/consultation question failed the provisional 0.70 cutoff and escalated despite relevant stored knowledge. Do not treat the lower token count on that request as an accuracy-preserving improvement. Calibrate RELEVANCE_THRESHOLD on representative questions before production.

Chat responses also include `knowledge_units`: the exact ordered knowledge context supplied to the answer LLM, with `id`, `title`, and `content`. This is populated from graph state by code, not generated by the model, and adds no LLM output tokens. It is an empty list when no knowledge was supplied and remains present for escalation outcomes. These are supplied context, not verified citations.

## Customer website chat

Public customer chat and a website widget are available through the frontend at `/c/{business-slug}`. The business portal’s **Integrations → Web Chat** section provides the link and embed script. Visitor sessions, message history and conditional lead capture are handled by [`src/public_chat`](src/public_chat/README.md).

### Per-reply AI usage

Business owners can read `GET /admin/{slug}/ai-usage` with their business JWT.
`start` and `end` are inclusive calendar dates in the business timezone; omitted dates
select this month through today. Optional `channel`, `offset`, and `limit` filter and
page the newest replies. Admin Agent Chat uses `admin_chat`; messaging integrations
use `instagram`, `whatsapp`, and `telegram`; visitor chat uses `web`.

Migration 014 adds `ai_usage_records`. Vercel container startup runs `alembic upgrade
head` before serving traffic, serialized with a PostgreSQL transaction advisory lock.
This only applies database migrations; it does not rebuild embeddings. Other deployment
methods must also apply migrations before running the new code.

Each actual agent invocation records the sum of provider-reported input/output tokens
across its model calls. Cached replies and duplicate webhook deliveries create no new
usage. Retries that invoke the agent again have separate records. Token counts exclude
embedding requests; incomplete provider metadata is flagged rather than estimated.
Duration includes agent processing and, for webhooks, the outbound send request (not
recipient delivery). Failed generations and failed sends are distinguished. Usage
starts at deployment; historic replies cannot be backfilled with accurate token counts.
A telemetry persistence failure logs `ai_usage_save_failed` or
`ai_usage_delivery_save_failed` without logging messages or credentials and without
forcing an already completed answer to be regenerated.

### Webhook Events portal

`GET /admin/{slug}/webhook-events` requires the owning business JWT. It supports
`source` (`instagram`, `whatsapp`, `telegram`), `status`, inclusive `start` / `end`
dates in the business timezone, and `limit` / `offset`. Responses include summary
counts, pagination, timestamps, delivery counts, processing durations and safe
failure explanations. They omit raw payloads, message text, sender identities and
channel credentials.

Migration 015 extends existing webhook claims without deleting them. New verified
message arrivals are saved before HTTP acknowledgment and move through `received`,
`processing`, and `processed` (send accepted) or `failed`. Non-text or invalid
messages are `ignored`; echoes, read receipts and delivery receipts are excluded.
Duplicate deliveries increment a count without re-running the agent or sending
another reply. The deduplication key includes business, channel account and sender,
since provider message IDs are not always globally unique. Both Instagram and
WhatsApp batches are isolated by account before processing.

Old claims have no business ownership and remain hidden; redelivery of an old
message is logged as an ignored legacy duplicate. No historic ownership or outcome
is inferred. Processing remains in the existing background-task mechanism: a worker
interruption can leave an event at Received or Processing without a final outcome.
This page does not automatically retry or manually replay messages.

### Daytime Gemini context cache

`GEMINI_CACHE_ENABLED=true` enables best-effort explicit caching of the agent's business instructions, system rules and current tool schemas. The first eligible request between `GEMINI_CACHE_START_HOUR=10` and `GEMINI_CACHE_END_HOUR=22` creates a cache that expires at the end hour on the same business-local day (Asia/Kolkata if no business timezone exists). There is no overnight creation or scheduled warm-up. Retrieved knowledge, the current time, customer history and the new message remain fresh on every request; the knowledge base stays in RAG.

Migration 018 stores only cache metadata and a short creation lease in PostgreSQL, so serverless workers share a cache without holding a database connection during provider calls. Cache identity includes tenant, credential fingerprint, model, instructions, tool schemas and schedule. Changed configurations replace the cached context; provider deletion is best-effort, and every cache has a fixed daily expiry. Concurrent requests that encounter another worker's creation lease proceed normally without creating another cache.

Gemini enforces its model-specific minimum cache size. Rejected configurations (including content below that minimum) use ordinary requests and are not retried until the configuration changes or the next day. Transient creation failures back off for five minutes. Cache operations have short timeouts; generation falls back once for a rejected/expired cache reference, but does not blindly retry generation timeouts, quota failures or server errors. Setting `GEMINI_CACHE_ENABLED=false` disables explicit cache use/creation; existing provider caches expire at their scheduled time.

AI Usage reports cached and uncached input counts from provider metadata, across all model rounds. Cached tokens are already included in total input tokens. Historical rows and responses lacking cache metadata remain unknown rather than being shown as zero. These counters are not a bill: cache creation/storage, output and fresh input are charged separately, and implicit cache hits can also contribute to cached input counts.
