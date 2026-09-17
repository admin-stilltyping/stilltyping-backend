# Demo business dashboard

This package owns the business-admin dashboard routes, widget catalog, synthetic
data generator, and saved configuration. All values are demos; no appointments,
orders, customers, or support records are created or read to populate the charts.

## Routes

Every route requires the existing business-admin bearer token. The business comes
from the verified account; callers cannot select another business by ID or slug.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin/dashboard?days=30` | Fetch 10 widgets, their demo data, and the catalog |
| GET | `/admin/dashboard/catalog?use_case=clinic` | List the 16 available widget types |
| POST | `/admin/dashboard/generate?days=30` | Generate and save another set of 10 |
| PATCH | `/admin/dashboard/config?days=30` | Save a custom selection of exactly 10 |

The period accepts `7`, `30`, or `90` days. Generation accepts
`{"use_case":"clinic","expected_revision":1}`. Customization accepts
`{"widget_ids":[...10 unique catalog IDs...],"expected_revision":1}`.
Use the revision returned in `config.revision`; a stale revision receives HTTP
409 so another browser's changes are not silently overwritten.

## Widgets and persistence

The four use cases are `clinic`, `retail`, `services`, and `support`. The initial
use case is inferred from the business name and description, with support as the
fallback. Each use case supplies relevant titles, categories, and sample units.

The catalog includes KPI, progress, gauge, sparkline, line, area, horizontal bar,
vertical bar, grouped bar, stacked bar, pie, donut, funnel, heatmap, ranked table,
and activity list. Random generation chooses two summary widgets, six charts, and
two detail widgets. Customization can choose any 10 unique types.

Migration `008` creates `dashboard_configs`, keyed by the business UUID. First
dashboard access initializes the saved selection, random seed, and sample end
date. Refreshing preserves these; regenerating changes the selection, seed, and
end date. Changing the sample period changes generated values without changing
the saved selection. Customizing preserves the seed and date. Values are stable
for each widget/use case/period/seed combination while generator code is unchanged.

PostgreSQL transactions use the existing business-slug advisory lock to serialize
initialization and changes. API responses disable caching. The seed is internal;
responses explicitly identify `mode: "demo"`.

## Frontend and local setup

The frontend is in `nivaso-frontend/web/src/features/dashboard/`, mounted at
`/dashboard` and used as the business-admin landing page. It has regeneration,
customization, period filters, accessible chart data tables, and mobile navigation.
API failures show errors and retry controls instead of fabricated fallback data.

Apply migrations with `uv run alembic upgrade head`, or rebuild the local API with
`docker compose up -d --build agent` (bootstrap applies migrations). Start the web
workspace with `npm run dev -w web` and sign in at
`http://<business-slug>.localhost:5173/login`.

```bash
uv run pytest -q tests/dashboard
uv run ruff check src/dashboard tests/dashboard migrations/versions/008_dashboard_configs.py
```

Tests cover all widget data contracts, stable samples, use cases, saved selections,
periods, revision conflicts, business isolation, and migration round trips. They
use SQLite and do not establish PostgreSQL concurrency guarantees. Real analytics
will require authenticated data providers and agreed business metric definitions.
