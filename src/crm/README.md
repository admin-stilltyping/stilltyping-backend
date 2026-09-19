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

Payment collection, tax/discount rules, stock accounting and staff/resource
availability are outside this implementation. Booking does not reserve an
exclusive staff slot. Orders remain a manual admin action.

## Chat appointment booking

The shared agent exposes `list_appointment_services` and `book_appointment` for
active businesses with Services/Appointments and Customers enabled. These tools
remain available across short follow-up messages without depending on semantic
tool retrieval or a tool-index resync. They work through every channel using
`Agent.run`, including Admin Agent Chat, public web chat and connected social DMs.

The agent looks up an active service, collects the patient's name, concern and
preferred local date/time, and calls the same appointment service used by the
owner API. Service price, duration and business identity come from the server.
Dates are interpreted in the business's saved IANA timezone; past, invalid and
ambiguous DST times are rejected. The system supplies current business time to
the agent for relative dates. Opening-hour and special-day rules come from the
business instructions; there is no structured calendar/availability validator.

Web/admin chat needs an international phone number. Social chat uses only the
server-provided channel and sender ID, optionally adding a patient-supplied
phone. Anonymous web session IDs are never treated as social identities. An
existing lead from the actual conversation is converted when Leads is enabled.
Patient name, channel and concern are retained in appointment notes.

Successful calls create **Scheduled** appointments pending staff confirmation,
visible immediately in the portal. The transaction also queues the existing
admin notification; a push is delivered only to opted-in devices. A save does
not prove notification delivery, doctor acceptance, or live slot availability.
Staff confirmation, rescheduling and cancellation continue in the admin portal;
the chat agent must not simulate those actions by creating another appointment.

The request ID is scoped to the actual chat turn and sender. Retrying it reuses
the saved appointment; changed details produce an error. Repeated confirmations
in a later message reuse an active appointment for the same contact, patient,
service and exact time. Patient names are kept separate so family members can
share a contact. The appointment, customer/lead conversion and notification are
atomic under the business lock. Disabled modules or foreign service IDs cannot
be bypassed with model arguments.

No database migration is required. Configure active, bookable services in
**Services** first, with the correct price and duration. For a consultation-first
clinic, create consultation services rather than inventing unapproved treatment
prices. If no active service matches, chat must direct the request to staff.

Tests: `tests/test_appointment_agent_tools.py` covers the agent-to-portal flow,
sender/tenant isolation, timezone handling, retries, conversion, notifications
and rollback, alongside the existing manual appointment tests.

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
