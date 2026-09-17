# Business module access

Super-admins configure access in **Businesses → business details → Modules**.
Selections are stored by business UUID in `business_modules`, independently of
plan metadata and custom field definitions.

## Rules

- Products + Orders share one switch.
- Services + Appointments share one switch.
- Either pair requires Customers. The UI selects Customers automatically; the
  API rejects inconsistent selections and a database constraint enforces the rule.
- Customers and Leads can be enabled independently.
- Support Tickets starts enabled and never requires a customer record.
- New businesses start with only Support Tickets enabled. Migration `011`
  preserves the previously available modules for existing businesses: both pairs,
  Customers, and Support Tickets. Leads starts disabled.
- Turning a module off blocks access without deleting its records or definitions.

## API

| Method | Path | Access |
|---|---|---|
| GET | `/super-admin/businesses/{slug}/modules` | Super-admin JWT |
| PUT | `/super-admin/businesses/{slug}/modules` | Super-admin JWT |
| GET | `/admin/businesses/{slug}/entitlements` | Owning business-admin JWT |

PUT takes all five boolean selections and the revision returned by GET:

```json
{
  "selection": {
    "product_orders": true,
    "service_appointments": false,
    "customers": true,
    "leads": false,
    "support_tickets": true
  },
  "expected_revision": 1
}
```

A stale revision returns 409 rather than overwriting another admin's changes.
Each save records the latest updater and increments the revision. This is not a
full change-history log. Responses are not cached.

## Enforcement

The portal loads authenticated entitlements before mounting pages, hides disabled
navigation entries, and protects direct module URLs. It checks for changes every
15 seconds and on refresh. Failed entitlement requests block access. Custom field
tabs follow the corresponding product/service switch.

Existing product and custom field APIs check access in the same per-business
transaction as their reads/writes. New module controllers should use
`module_transaction` to retain that guarantee. Support management requires the
owner's token and matching tenant slug; list/detail/update and AI ticket creation
also check Support Tickets access. Unregistered standalone agent tenants retain
their pre-existing ticket-creation fallback; their management endpoints now
require a registered business owner.

The [CRM and transaction flow](../crm/README.md) provides enquiry capture, minimal
customer identities, service management, orders and appointments. These APIs use
the same module transaction checks. Other legacy tenant APIs retain their previous
access behavior.

Tests: `tests/test_modules.py` and `tests/test_module_migration.py`, alongside the
existing product/custom-field and support tests.
