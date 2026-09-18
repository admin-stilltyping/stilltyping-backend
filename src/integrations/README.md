# Business integrations

Instagram setup is available in the business portal under **Integrations → Instagram**. Settings use the existing `channel_accounts` table; no migration is needed. Instagram setup is available to active business owners through the `channel.instagram` capability. CRM module choices remain independent.

## Owner API

- `GET /admin/{slug}/channels`: safe configuration summaries for the signed-in business. Credentials are never returned. Telegram's secret routing ID is also omitted.
- `PUT /admin/{slug}/channels/instagram`: save a numeric `account_id`, Instagram Login `access_token`, Meta `app_secret`, and a chosen `verify_token`.
- `DELETE /admin/{slug}/channels/instagram`: remove the local routing/credential record. Saved conversations and enquiries remain.

All endpoints require a business-admin JWT for the URL's business. Initial setup requires all credentials. Updates may omit or leave credential fields blank to retain the existing values. Switching account IDs requires disconnecting first. An account already assigned to another business cannot be reassigned by an owner. The portal supports one Instagram account per business; existing multi-account CLI configurations must be resolved by the platform admin before editing here.

The response's `configured` value means all required settings are saved. It does not assert Meta token validity or webhook subscription. The UI labels this **Setup saved**. Saving performs no external API requests and does not send messages, register subscriptions or implement an OAuth flow. Credentials remain in the server's existing channel config store and are not echoed to the frontend.

## Meta setup

This uses the existing Instagram API with Instagram Login adapter. Use an Instagram professional account and obtain the app/account's messaging permissions, including `instagram_business_manage_messages`. Register a publicly reachable HTTPS callback ending in `/webhooks/instagram` with the same verify token, and subscribe the account to message notifications. Follow [Meta's Instagram Login API collection](https://www.postman.com/meta/instagram/folder/1z5vxzu/instagram-api-with-instagram-login) and [the messaging guide](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/) for current setup and access requirements.

The frontend displays a callback based on `VITE_PUBLIC_API_BASE_URL`, falling back to `VITE_API_BASE_URL` and the portal origin. Set `VITE_PUBLIC_API_BASE_URL` to the public backend base URL when it differs from the portal and restart Vite. Localhost callbacks are clearly marked for local testing only. Vite and the production web Vercel configuration proxy `/webhooks/instagram` to the backend. This exact callback route leaves the portal's `/webhooks` event-list page available.

## Incoming messages

The existing adapter validates Meta HMAC signatures, parses text DMs and ignores message echoes. Instagram webhook batches are split by account after checking the original signed body, so a batch containing multiple business accounts cannot mix their enquiries or replies. Inactive registered businesses are ignored. Repeated message IDs are deduplicated by the existing webhook pipeline. When Leads is enabled, enquiries retain the Instagram sender ID and do not create customers by themselves.

Tests use fake credentials and mocked outbound sends. No real Instagram account is contacted. Live testing requires the owner's Meta credentials and public callback setup. Media attachments, comments, OAuth onboarding and subscription management are outside this change.
