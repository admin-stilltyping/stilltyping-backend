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
