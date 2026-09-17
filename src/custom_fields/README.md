# Custom fields

Definitions are scoped by `(business_id, entity_type, key)`; businesses can use
the same key with different types/options. The business UUID is derived from the
verified business-admin session. Definitions support `product` and `service`;
service record storage remains a separate milestone.

Base: `/admin/{slug}/custom-fields/{entity_type}`

- GET: active definitions sorted by order and key. `include_archived=true` also
  returns archived definitions for displaying historical product values.
- POST: create a definition with key, label, type, required, order and options.
- PATCH `/{id}`: update label, required, order and options. Keys and types are
  immutable; existing option values must remain, but labels can change and new
  values can be added.
- DELETE `/{id}`: archive. Values in existing records are preserved. Archived
  keys remain reserved and definitions cannot be edited.

Supported types: text (up to 4,000 characters), finite number (absolute value at
most 10^15), boolean, ISO date (YYYY-MM-DD), select and multiselect. Select options
are unique and limited to 100. A business/entity can have 100 active definitions,
200 including archived definitions. Keys use lowercase letters, digits and
underscores, begin with a letter, and are at most 64 characters. Prototype keys
are rejected. Unknown request properties are rejected.

The shared `validate_attributes` function implements product validation. New
entity APIs should call it under the same business transaction lock.
