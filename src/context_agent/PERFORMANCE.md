# Portal request timing

Business owners can opt into diagnostics on authenticated `GET /auth/me` and
`GET /admin/...` requests by sending `X-Request-Timing: 1`. Ordinary requests do
not acquire a connection eagerly or emit diagnostic headers/logs.

`Server-Timing` contains milliseconds measured inside the application, up to
response headers:

| Metric | Meaning |
| --- | --- |
| `db_acquire` | Connection checkout: pool wait, connection health check, and connection setup when needed. |
| `db_query` | SQL driver calls, including database transport, execution, implicit transaction start, and buffered results. |
| `db_lock` | Advisory-lock driver calls, including database transport and any lock contention. |
| `db_finish` | Commit/rollback and returning the connection to the pool. |
| `app` | Remaining application wall time, including ORM/serialization/scheduling; not a CPU measurement. |
| `total` | Total application wall time before response headers. |
| `db_count` | Number of SQL calls, excluding advisory locks; carried as a description, not a duration. |

Overlapping intervals are partitioned rather than added twice. `X-Timing-ID`
correlates headers with a structured `request_timing` log. Logs contain a route
template, status, and numeric timings; no SQL, parameters, account identifiers,
request headers, response bodies, database URLs, or credentials.

`GET /admin/{slug}/diagnostics/database` requires the verified owner of `{slug}`.
It runs read-only `EXPLAIN ANALYZE` for seven fixed PostgreSQL SELECTs matching
the Services page's database reads. Results expose only timing values:

- `postgres_planning_ms` / `postgres_execution_ms`: measured by PostgreSQL itself.
- `round_trip_ms`: the application's wait for that diagnostic SQL call.
- `transport_driver_remainder_ms`: round trip minus database planning/execution;
  includes transport and driver overhead, not a pure network measurement.

The endpoint does not accept client SQL, change database settings, or return query
plans or row data. EXPLAIN measurements are separate diagnostic executions and
must not be presented as server-side timings of an earlier API request.

Compare application `total` with Vercel's invocation duration and client elapsed
time to locate time outside the application. A slow SQL driver call alone does
not establish a slow PostgreSQL query; use the database-side EXPLAIN timings to
distinguish query execution from transport/connection overhead.

## Deferred performance work — 2026-09-19

The business owner asked to keep these findings for later and prioritize push
notifications. No performance optimization has been applied.

- The Services page fetches custom-field definitions immediately, although they
  are used only by the New Service form. Defer that request until the form opens.
  Earlier production samples had a 3.16-second median for this request; this is
  request duration, not guaranteed page-load savings.
- Initial page loading waits for `/auth/me`, then entitlements, then page data.
  The supplied browser sample spent 1.59 + 2.85 = 4.44 seconds on those first two
  requests. Consider a combined bootstrap response while preserving server-side
  account, tenant, and module authorization.
- Tenant transactions take an exclusive PostgreSQL advisory lock even for
  read-only list requests. Review read concurrency separately from mutations;
  preserve write ordering and module-disable consistency guarantees.
- Authentication re-reads the account and business for every request. The
  Services path performs five SELECTs across two transactions, plus its advisory
  lock. Measure connection/transport costs before choosing joins or caching.
- Direct-vs-portal samples showed about 0–40 ms difference, so the frontend proxy
  was not the main contributor in those samples.

The query execution / connection / transport / application split was not yet
measured in production: diagnostic deployment `7639025` failed internally in
Vercel, and its replacement was queued at the last check. Do not treat total API
duration as PostgreSQL execution time or assume the database region is known.

## Production connection exhaustion — 2026-09-19

During the chat-booking rollout, the existing deployment `01d66b5` failed at
startup in Alembic with `(EMAXCONNSESSION) max clients reached in session mode`,
reporting a pool size of 15. This was a connection-capacity outage, separate from
the deferred query-performance work above.

`DATABASE_POOLER_MODE=transaction` is an explicit opt-in for a Supabase asyncpg
pooler URL. Settings preserves the database, credentials, host and SSL options,
changing only the port to 6543. Both migrations and application transactions use
the same engine builder: NullPool, disabled asyncpg/SQLAlchemy prepared-statement
caches, and unique statement names. Ordinary PostgreSQL and SQLite retain their
configured behaviour. Business locks are transaction-scoped and remain intact.

This follows [Supabase's SQLAlchemy guidance](https://supabase.com/docs/guides/troubleshooting/using-sqlalchemy-with-supabase-FUqebT)
and [SQLAlchemy's asyncpg pooler guidance](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#prepared-statement-name-with-pgbouncer).
It does not raise database capacity, terminate sessions, or change query/tenant
locking semantics. Old deployment connections may take time to drain.
