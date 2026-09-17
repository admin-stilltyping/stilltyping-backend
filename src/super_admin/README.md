# Super-admin authentication

This package owns platform administrator authentication. It shares the existing
FastAPI process, PostgreSQL connection, and Alembic migration history with
`context_agent`. The wheel includes both `src/context_agent` and `src/super_admin`.
New super-admin features can live in this package without moving tenant agent code.

## Implemented routes

| Method | Path | Authentication |
|---|---|---|
| POST | `/auth/super-admin/login` | Username and password in a JSON body |
| GET | `/auth/super-admin/me` | `Authorization: Bearer <access_token>` |

Login accepts the existing admin frontend's JSON contract:

```json
{"username": "superadmin", "password": "your chosen password"}
```

A successful response contains `access_token`, `token_type: "bearer"`, and
`expires_in` (seconds). `/me` returns `id`, `username`, and `role: "super_admin"`.
Passwords and password hashes are never returned. Auth responses use `Cache-Control:
no-store`. Wrong passwords, unknown users, disabled users, and locked users all
receive the same 401 error envelope:

```json
{"error": {"code": "invalid_credentials", "message": "Invalid username or password."}}
```

Malformed input returns 400 using the application's existing validation handler.
A missing or shorter-than-32-byte signing secret returns 503 with
`error.code: "auth_not_configured"`; the application has no default login or key bypass.

## Setup on the host

Run from the backend project directory:

```bash
cd /Users/muthuraman/Desktop/context-agent
uv sync --frozen
```

Generate a signing secret:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Copy the generated value into **the backend's** `.env` as
`SUPER_ADMIN_JWT_SECRET=<generated value>`. Keep this key on the server; it does not
belong in the frontend or in a `VITE_` variable. Leave any existing `.env` settings
in place. The other settings are optional:

```dotenv
SUPER_ADMIN_TOKEN_MINUTES=60
SUPER_ADMIN_LOGIN_MAX_ATTEMPTS=5
SUPER_ADMIN_LOGIN_LOCK_SECONDS=900
```

With PostgreSQL running and `DATABASE_URL` pointing to the intended database:

```bash
uv run alembic upgrade head
uv run context-agent-super-admin create --username superadmin
```

The command prompts twice for a hidden password of 15–128 characters. There is no
public signup route, password command-line argument, or default super-admin account.
Usernames are case-insensitive and contain 3–64 letters, digits, dots, underscores,
or hyphens. Passwords preserve whitespace and are stored only as salted Argon2id hashes.
Account provisioning needs the database, not a running Gemini or Qdrant service.

Restart the backend process to load the new code and secret:

```bash
uv run uvicorn context_agent.api:app --host 127.0.0.1 --port 8000
```

Use this startup command when running the backend on the host; its normal Gemini
and storage configuration still applies. If port 8000 is an SSH tunnel to another
machine, modifying this checkout does not update that machine. Deploy the package
and migration on the actual backend host, or deliberately point the local frontend
at a local backend instance.

Then open `http://localhost:5174/login` and enter the account you created. The
frontend's existing `/auth` development proxy targets backend port 8000.

## Setup with the existing Docker Compose stack

Set the backend `.env` signing secret as above, then rebuild the agent service:

```bash
docker compose up -d --build agent
docker compose exec agent context-agent-super-admin create --username superadmin
```

The agent startup applies migrations through `007` (super-admin and business accounts). Wait for startup to complete before
creating the account. Compose passes the four authentication settings into the
container. The account command uses the container's database configuration.

## Password recovery and session behavior

```bash
uv run context-agent-super-admin reset-password --username superadmin
# Or, for the Compose stack:
docker compose exec agent context-agent-super-admin reset-password --username superadmin
```

A password reset clears the login lock and increments the account's token version,
immediately invalidating its old tokens at protected backend routes. Disabling
`super_admins.is_active` also prevents login and rejects existing tokens. A reset
does not reactivate a disabled account.

Tokens expire after 60 minutes by default. There is no refresh-token route; sign in
again after expiry. The current frontend logout clears its local token. It does not
revoke a copied token at the server; reset the password to revoke all account tokens.
Rotating the signing secret invalidates every super-admin and business-admin token.

After five consecutive failed passwords, the account is locked for 15 minutes by
default. The counter is stored in PostgreSQL and row-locked across workers. Success
or expiry of the lock resets it. Attempts while locked do not extend the lock.
The outward login error remains generic to avoid disclosing account state.

Before public deployment, use HTTPS and edge request-rate limits; account lockout
does not limit total requests to unknown usernames. This change does not add MFA.

## Adding protected super-admin features

Use `require_super_admin` for every future platform endpoint:

```python
from fastapi import APIRouter, Depends
from super_admin.routes import require_super_admin

router = APIRouter(
    prefix="/super-admin",
    dependencies=[Depends(require_super_admin)],
)
```

The dependency validates the signature, algorithm, issuer, audience, expiry, role,
account activity, and token version. A client-provided role or shared key cannot
grant access. Keep the login router separate because login must be reachable
without a token.

Business creation and owner login are implemented in the [business package](businesses/README.md).
That guide covers generated IDs/slugs, the one-time initial owner password, portal
routes, local subdomains and operator password recovery. This guide's login response
contract above describes super-admin authentication only. The existing tenant
`/api/v1` routes retain their previous access behavior; full tenant authorization,
feature controls, live analytics and other platform APIs remain separate work.
The authenticated [demo dashboard](../dashboard/README.md) now lives in its own
package and uses synthetic values.

## Validation

```bash
uv run pytest -q tests/super_admin
uv run pytest -q
uv run ruff check src tests migrations
uv run alembic upgrade head --sql
```

Auth tests use temporary SQLite storage and real password hashing/JWT validation.
They cover the frontend login contract, failed login persistence/lock expiry,
missing secrets, invalid/expired/forged tokens, disabled accounts, password-reset
revocation, account validation, and CLI password handling. SQLite tests do not
establish PostgreSQL concurrency behavior.

Library references: [FastAPI password hashing and JWT guide](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/),
[PyJWT validation API](https://pyjwt.readthedocs.io/en/stable/api.html),
[pwdlib API](https://frankie567.github.io/pwdlib/reference/pwdlib/).
