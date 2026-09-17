# Business provisioning and portal login

The `super_admin.businesses` package owns the business registry and the first admin
account for each business. Migration `007` adds `businesses` and `business_admins`.
Both records are inserted in one database transaction.

## Identity

- `_id`: a server-generated, permanent UUID, stored as `businesses.id` in PostgreSQL.
- `slug`: a unique, immutable DNS label used for subdomain routing and the existing
  string `tenant_id` APIs. UUIDs do not replace legacy tenant IDs in this migration.
- Automatic slugs are derived from the business name. Accents are normalized,
  separators become hyphens, and collisions receive numeric suffixes. Names with
  no ASCII letters/digits fall back to `business`.
- An explicit slug must contain 3–63 lowercase ASCII letters/digits/hyphens, with
  no leading/trailing hyphen. Reserved application names cannot be used.
- Suggestions are advisory; creation checks again under the tenant lock and a
  database unique constraint. A concurrent conflict returns 409; choose again.
- Slugs already used by legacy tenant data are also unavailable. Existing records
  are not silently assigned to a newly created owner or migrated automatically.

## Routes

| Method | Path | Access |
|---|---|---|
| GET | `/super-admin/businesses/suggest-slug?name=Bright%20Smile` | Super-admin JWT |
| POST | `/super-admin/businesses` | Super-admin JWT |
| GET | `/super-admin/businesses` | Super-admin JWT |
| GET | `/super-admin/businesses/{slug}` | Super-admin JWT |
| PATCH | `/super-admin/businesses/{slug}/status` | Super-admin JWT |
| PATCH | `/super-admin/businesses/{slug}/plan` | Super-admin JWT |
| GET | `/web/businesses/{slug}` | Public name, slug and `_id` for an active business |
| POST | `/auth/login` | Business slug, username and password |
| GET | `/auth/me` | Business-admin JWT; verified account and saved business |
| GET | `/admin/businesses` | Business-admin JWT; only the caller's business |
| GET | `/admin/businesses/{slug}` | Business-admin JWT; 403 for another business |

Creation body:

```json
{
  "name": "Bright Smile Dental",
  "slug": "bright-smile-dental",
  "description": "Dental care in Chennai",
  "timezone": "Asia/Kolkata",
  "plan": "free"
}
```

`slug` may be omitted for automatic generation. Creation returns 201 with `_id`,
`slug`, `name`, `description`, `timezone`, `status`, `plan`, `created_at`, `updated_at`,
plus `admin_username` and `admin_password`. Only the creation response contains
that generated password. GET responses never contain credentials or hashes.
The frontend shows the password once and does not put credentials into portal URLs.

Login body:

```json
{"business_slug": "bright-smile-dental", "username": "bright-smile-dental", "password": "the generated password"}
```

Login returns an `admin` token with audience `business-admin`, account ID, business
ID and business slug. The backend verifies those claims against active stored
records on every protected request. A super-admin token and shared API keys cannot
stand in for the owner's token. Public branding does not expose the saved description
or other profile details. The business frontend takes its slug from the configured
subdomain and uses `/auth/me` to verify the session before mounting private pages.
Query parameters and browser-selected tenant state cannot change that identity.

Status accepts `active`, `suspended` or `inactive`; disabling access also revokes
existing owner tokens. Reactivating a business requires a fresh sign-in. Plan accepts
`free`, `starter`, `pro`, `enterprise` or null. Plan metadata is persisted.
[Module access](../../modules/README.md) is configured separately by the super-admin;
billing and plan limits remain a separate milestone.

The business login shares the server-only signing secret, token duration and lockout
settings documented in [the auth guide](../README.md), with separate role/audience
checks and separate account tables. Passwords are hashed using Argon2id.

## Recover a lost initial password

The operator can replace a lost password using hidden terminal prompts:

```bash
docker compose exec agent context-agent-super-admin reset-business-password --username bright-smile-dental
# Host development:
uv run context-agent-super-admin reset-business-password --username bright-smile-dental
```

A reset clears the login lock, revokes old tokens, and does not activate suspended
businesses or disabled accounts. No public password-reset route is exposed.

## Local browser flow

1. Start the backend (`docker compose up -d --build agent`). Startup migrates to head.
2. In the frontend repository run `npm run dev:admin` and `npm run dev:web` in separate terminals.
3. Open `http://localhost:5174/businesses`, sign in as super-admin and create a business.
4. Review or edit the generated slug before saving. Save the returned login details.
5. Open `http://<slug>.localhost:5173/login` and sign in with those credentials.
   The portal opens the [demo dashboard](../../dashboard/README.md). Use
   **Business Profile** in the navigation to view the saved `_id` and details.

The root `localhost:5173/login` also accepts a business slug for manual local access.
The browser session is scoped to its origin; opening a different subdomain requires
sign-in there. Vite proxies `/auth`, `/web` and `/admin` to the backend.

For production set admin `VITE_WEB_URL=https://example.com` and web
`VITE_TENANT_BASE_DOMAIN=example.com`, configure wildcard DNS/TLS and route tenant
subdomains to the web app. Proxy API paths to this backend; Vite's proxy only runs
in development. The backend authorizes using the verified token, not an untrusted
Host header. Secrets must never be placed in frontend `VITE_` variables.

## Scope and tests

This milestone implements creation, listing, profile viewing, plan/status metadata
and owner login. Business editing, signup approval, billing and
other unfinished platform screens remain separate work. The existing `/api/v1`
agent routes retain their previous access behavior: **this is not authorization
for all tenant APIs**. Apply owner authorization to those routes before exposing
them publicly. No existing tenant data is renamed or deleted.

Run `uv run pytest -q tests/super_admin` for validation, duplicate and legacy slug
conflicts, atomic rollback, password hashing, login/lockout, role isolation, tenant
isolation, token revocation and migrations. Tests use isolated SQLite databases;
PostgreSQL migrations and the browser flow should also be checked locally.
