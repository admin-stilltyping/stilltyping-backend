# Products

Authenticated business-admin catalog backed by PostgreSQL. Product UUIDs and
ownership come from the backend; a URL slug must match the signed-in business.
Every record lookup includes the business UUID. Platform tokens cannot use these
owner routes. Products and custom field definitions use a shared per-business
transaction lock to serialize writes and schema changes.

## API

Base: `/admin/{slug}/products`

- `GET /`: `{items, total, catalog_total, categories}`. `limit` 1–100 (default 25),
  `offset`, `search` (name/SKU/description), `category`, and `status` filters are
  applied in the database. `all` includes archived products. Categories and
  catalog count include only the current business. Results have a stable order.
- `POST /`: name, price, optional currency (INR/USD), SKU, category, description,
  status and attributes. Returns 201 with the saved record.
- `GET /{id}`: one owned product, or 404.
- `PATCH /{id}`: partial update. Omitted fields remain unchanged. Blank/null SKU,
  category and description clear them. Currency is fixed after creation.
- `DELETE /{id}`: archive, preserving the record and attributes; returns 204.

Price uses `NUMERIC(14,2)` and is serialized as a decimal string. Negative,
non-finite and more-than-two-decimal prices are rejected. Nonempty SKUs are
unique within a business, including archived products.

Custom values live in `attributes` JSONB. Every submitted key must belong to the
business's product definitions. PATCH merges keys; null or an empty string/list
clears optional values. Zero and false are valid required values. Archived
attributes are preserved and read-only, including when omitted from a PATCH.
New required definitions apply on subsequent product creates/updates; existing
products are not silently rewritten. Unknown fields and wrong types fail before
any product changes are committed.

Migration: `010_products_custom_fields.py`. Tests: `test_products_custom_fields.py`
and `test_product_migration.py`.

The [super-admin module switches](../modules/README.md) gate all catalog routes
and product field definitions. This provides catalog storage; stock/variant
accounting and AI product retrieval remain future work. The
[order flow](../crm/README.md) now uses active products and preserves saved prices
when the catalog changes.
