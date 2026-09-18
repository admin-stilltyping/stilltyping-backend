# Business admin notifications

New support tickets, appointments, and orders create a notification for their
owning business. Customer messages and leads do not trigger notifications.
The current data model has one business-admin account per business. Customers
and platform super-admins are not subscribed by this feature.

The portal bell lists the 30 latest alerts, keeps an unread count, supports mark
all/read and links to each record. Browser push is a separate, explicit opt-in
on each device (up to ten devices per admin). The browser permission prompt is
shown only after clicking Enable. Signing out or turning push off revokes the
browser subscription and clears the service worker's device binding. Expiring
the login session alone does not revoke the device's push opt-in; opening a
notification still requires a valid business login. Account deactivation or
token-version revocation prevents future dispatch to its old subscriptions.

## Provider and setup

This uses the standard Web Push API with VAPID and `pywebpush`, not a Firebase
SDK/project. The browser chooses its own vendor push endpoint. The backend
accepts only HTTPS endpoints under the supported Google, Mozilla, Apple and
Windows push-service hosts and never follows delivery redirects.

1. Generate a private key file outside source control:
   `python -m context_agent.push_keys /private/path/push.env`.
2. Set backend `PUSH_VAPID_PRIVATE_KEY` to that raw base64url P-256 scalar and
   `PUSH_VAPID_SUBJECT` to a monitored contact mailto: or public HTTPS URL.
   Keep the same private key across deployments. Never use a `VITE_` variable.
3. Deploy backend migration 016 before deploying the frontend. The Docker
   entrypoint already runs `alembic upgrade head`.
4. Serve the frontend over HTTPS with `/push-sw.js`, manifest and PNG icons.
   Open the bell and choose Enable on this device, then allow the browser prompt.
   On iPhone/iPad, use the portal installed on the Home Screen.

Without the key/contact configured, the in-app bell still works and explains
that browser notifications are not configured. No device permission is requested.

## Delivery and privacy

An alert and its per-device delivery jobs are saved in the same transaction as
the new record. Creation retries reuse the existing record and do not enqueue a
second alert. Only devices subscribed when the event was created receive pushes.
Payloads contain a generic title/body, internal destination and IDs; no customer
message, name, phone number, appointment notes, order contents or credentials.

The request awaits a bounded delivery pass after its response body, outside
business transactions. A background worker also recovers work on startup and
checks retries every minute while the service is running. PostgreSQL row locks
and expiring leases coordinate workers. Delivery errors retry with exponential
backoff, up to five attempts within one day. 404/410 removes expired devices.
Provider acceptance is not proof of display on a device. Delivery is at-least-
once: a crash after provider acceptance may retry; the browser notification tag
replaces the same alert without re-alerting.

If the hosting platform suspends/stops the container, scheduled retries resume
when it next starts or handles an alert-producing request. This is not a managed
always-running queue. A separately scheduled worker is needed if retry deadlines
must be guaranteed during long periods without traffic.

Relevant standards: [Push API](https://developer.mozilla.org/en-US/docs/Web/API/Push_API),
[permission/subscription](https://developer.mozilla.org/en-US/docs/Web/API/PushManager/subscribe),
[iOS Home Screen web push](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/).
