# Customer web chat

Public routes live in this package. Visitors do not use business-admin JWTs.

## Using the chat

- Open the web frontend at `/c/{business-slug}`. The old `/chat/{business-slug}` link also works, except the existing reserved admin demo route `/chat/demo`.
- In the business portal, open **Integrations → Web Chat** for a copyable public link and website script.
- The script at `/chat-widget.js` creates a bottom-right launcher and loads `/c/{slug}?embed=1` in an iframe only when opened. Closing preserves the conversation.
- Business name comes from the saved, active business record. AI replies use the existing agent, instructions, knowledge base and server model configuration.
- Messages become anonymous web enquiries when Leads is enabled. Enquiries do not create customers. Phone/social identity is still required when converting a lead through an order/appointment.

## API

All routes use `/web/businesses/{slug}`:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/config` | Public business name, slug and message length limit |
| POST | `/sessions` | Issue an anonymous visitor token, valid for 30 days |
| GET | `/messages` | The token's latest 100 turns, oldest first |
| POST | `/messages` | Send `{request_id: UUID, message: string}` |

Use `Authorization: Bearer <visitor-token>` for both message routes. Tokens are randomly generated on the server and stored as SHA-256 hashes. They are scoped to one business and visitor; no public session directory or arbitrary visitor ID lookup is provided. Requests cannot choose a model, admin mode, channel, external identity or another tenant. Replies expose only message text/status, never retrieved knowledge, tools or credentials. Responses use `Cache-Control: no-store`.

The frontend saves its visitor token per business in browser storage, with an in-memory fallback when third-party storage is blocked. Starting a new chat forgets the browser token; it does not delete the business's saved enquiries. Reloading a browser that blocks storage starts a fresh session.

## Persistence and retries

Migration `013` adds `web_chat_sessions` and `web_chat_turns`. A per-session sequence preserves turn order. The existing database tenant lock serializes acceptance of a turn; a pending turn prevents concurrent sends within the same visitor session. The reply is committed atomically with the agent conversation's message pair through `Agent.run(persist_response=...)`.

Clients reuse the same request ID when retrying. Completed requests return the saved reply. Changed text for the same request ID returns 409. A failed AI run leaves the enquiry intact and can be retried without another enquiry. Provider errors are not exposed to visitors. Reloads recover pending/completed/failed turns from the server. A hard process termination may leave a pending turn; the visitor can start a new chat. No automatic replay of an uncertain agent turn is attempted.

## Serving the widget

Serve the frontend and `/web/*` API through the same HTTPS origin. Vite already proxies `/web` locally. Production routing must proxy `/web/*` to the backend and serve `/c/*` through the SPA fallback. Serve `/chat-widget.js` as JavaScript. Allow customer website origins to frame `/c/*`; keep admin pages protected from framing. The host website's CSP must allow the widget script and iframe origin. No third-party cookies, parent-page DOM access, or admin token sharing is required. If `VITE_API_BASE_URL` points to another origin, that API needs explicit CORS configuration; same-origin proxying is preferred.

The public routes apply per-worker burst limits of 10 new sessions and 30 generated replies per IP/minute. Cached retries are free. For a multi-worker public deployment, enforce shared edge limits and configure trusted proxy IP forwarding. This module does not change the older `/api/v1` authentication policy; deployment must restrict legacy management endpoints separately.

## Verification

Run `pytest tests/test_public_chat.py tests/test_public_chat_migration.py` for privacy, expiry, tenant isolation, payload validation, stable history, atomic persistence, deduplication, concurrent sends, model failures and lead-switch behavior. The model is stubbed in tests while the actual agent history/enquiry persistence runs.
