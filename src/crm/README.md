# Enquiries, customers, and transactions

An incoming enquiry creates or updates a lead. It does not create a customer.
The first successfully saved order or booked appointment creates a customer (or
reuses an existing match) and marks the lead converted. Cancellation preserves
the customer and historical enquiries.

## Identity

- Contacts hold an optional international phone number and zero or more social
  identities, each with a platform and external ID. Names/email are not required
  or stored in this model.
- Customers have a server-generated UUID and reference a contact. At least one
  phone/social identity is required at conversion; anonymous web visitors remain
  leads until contact details are supplied with an order/appointment.
- Phone formatting is normalized to `+<country code><number>`. No country is
  guessed. Social IDs are platform-scoped opaque IDs, not inferred from names.
- Matching is exact and restricted to a business UUID. Conflicting identities
  return 409; the system does not silently merge two people. Identities can be
  enriched during conversion, not removed or overwritten.
- Lead sources group repeated messages from the same web session/social sender.
  Enquiries retain their message, channel and timestamp after conversion.

## Routes

All routes below start with `/admin/{slug}`, require the owning business-admin
JWT, and check the business's saved module selection. Lists return
`{items, total}` and support bounded `limit`/`offset` pagination.

| Module | Routes |
|---|---|
| Customers | `GET /customers`, `GET /customers/{id}`, `GET /customers/{id}/leads` |
| Leads | `GET /leads`, `GET /leads/{id}`, `GET /leads/{id}/enquiries`, `POST /leads/enquiries` |
| Services | `GET/POST /services`, `GET/PATCH/DELETE /services/{id}` |
| Orders | `GET/POST /orders`, `GET/PATCH /orders/{id}` |
| Appointments | `GET/POST /appointments`, `GET/PATCH /appointments/{id}` |

There is no create-customer or manual convert-lead endpoint. Customer creation
is owned by the transaction flow. Customer lists search phone/social IDs;
lead lists additionally filter enquiry/converted status. Order and appointment
lists support `customer_id` and status filters. Service lists search the catalog
and filter status. A customer's enquiry links also require Leads enabled.

Orders and appointments accept `request_id` and exactly one of:

- `customer_id`: an existing customer;
- `lead_id`: a lead, optionally with `contact` details to identify an anonymous enquiry;
- `contact`: phone and/or `social_identities` to create/reuse a customer directly.

Enquiries also use `request_id`. Retrying the same payload returns the existing
record; reusing that request ID with a different payload returns 409. Keep the
request ID while retrying an uncertain save. UUIDs and business IDs are assigned
by the server. Per-business transaction locks serialize conversion and writes;
composite foreign keys enforce tenant ownership of relationships in the database.
Failed transactions roll back customer creation, contact enrichment and conversion.

## Catalog and transaction rules

Orders accept product IDs and positive integer quantities. The server loads
active products, rejects mixed currencies, and calculates Decimal totals. Saved
line items retain product names, SKUs and unit prices after catalog changes.
Orders start `confirmed` and can become `fulfilled` or `cancelled`.

Appointments require an active service and a future timezone-aware timestamp.
Service name, price, currency and duration are copied into the booking. Active
appointments can be rescheduled, confirmed, completed, cancelled or marked no-show;
finished/cancelled records cannot be changed. The portal labels the device time
zone used for input and display. Stored timestamps use UTC semantics.

Services support the same tenant-specific field definitions and six validated
field types as products. Archiving keeps service definitions, custom values and
past appointment snapshots. Currency is immutable after service creation.

This is manual order/booking management. Payment collection, tax/discount rules,
stock accounting, staff/resource availability and automatic AI transaction tools
are outside this implementation. Booking does not reserve an exclusive staff slot.

## Incoming enquiry capture

`Agent.run` captures accepted incoming messages before invoking the model, so
enquiries survive model failures. This covers the existing web/API chat path and
verified WhatsApp, Instagram and Telegram webhook paths that call the agent.
Capture only runs for active registered businesses with Leads enabled. Existing
unregistered agent tenants retain their previous behavior. Web enquiries do not
treat anonymous session IDs as social identities. This captures new enquiries;
it does not migrate historical conversation messages or initiate external messages.

Support tickets stay independent. Raising a ticket does not create a customer;
the underlying incoming enquiry can still appear as a lead when Leads is enabled.

## Validation and rollout

Migration `012_crm_transactions.py` adds the new tables without changing existing
businesses, module settings or catalog records. Backend implementation is grouped
under `src/crm`, `src/services`, `src/orders` and `src/appointments`. Portal flows
live in `web/src/features/crm` and reuse the existing service catalog forms.

Tests: `tests/test_crm_transactions.py` and `tests/test_crm_migration.py`, plus the
existing module, catalog, agent and support suites. Migration tests enforce tenant
foreign keys, phone uniqueness per business and downgrade isolation.
