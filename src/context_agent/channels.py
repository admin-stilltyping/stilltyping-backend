"""Messaging-channel adapters: normalize each platform's webhook and reply API.

Adapters are pure with respect to the database — they parse raw bytes and headers,
verify signatures, and make outbound HTTP calls. Tenant resolution, idempotency and
the agent turn live in webhooks.py. No URL or credential ever comes from LLM output;
credentials are read from the tenant's ChannelAccount.config.
"""

import hashlib
import hmac
import json
import logging

import httpx

log = logging.getLogger(__name__)

META_GRAPH = "https://graph.facebook.com"
INSTAGRAM_GRAPH = "https://graph.instagram.com"
TELEGRAM_API = "https://api.telegram.org"
GRAPH_VERSION = "v21.0"


class Inbound:
    """One normalized inbound text message."""

    __slots__ = ("external_user_id", "text", "message_id")

    def __init__(self, external_user_id: str, text: str, message_id: str):
        self.external_user_id = external_user_id
        self.text = text
        self.message_id = message_id


def _loads(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def _verify_meta(config: dict, raw: bytes, header: str | None) -> bool:
    secret = config.get("app_secret") or ""
    if not secret:
        # No app secret configured: cannot verify. Refuse rather than trust blindly.
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def _meta_challenge(params: dict, config: dict) -> str | None:
    # Require a configured verify token: an absent stored token must never match an
    # absent request token (None == None) and echo an attacker-supplied challenge.
    expected = config.get("verify_token")
    if expected and params.get("hub.mode") == "subscribe" and params.get(
        "hub.verify_token"
    ) == expected:
        return params.get("hub.challenge")
    return None


class WhatsAppAdapter:
    name = "whatsapp"
    has_challenge = True
    verify_field = "verify_token"

    def routing_key(self, headers, raw: bytes) -> str | None:
        try:
            value = _loads(raw)["entry"][0]["changes"][0]["value"]
            return value.get("metadata", {}).get("phone_number_id")
        except (KeyError, IndexError, TypeError):
            return None

    def challenge(self, params: dict, config: dict) -> str | None:
        return _meta_challenge(params, config)

    def verify(self, headers, raw: bytes, config: dict) -> bool:
        return _verify_meta(config, raw, headers.get("x-hub-signature-256"))

    def parse(self, raw: bytes) -> list[Inbound]:
        out: list[Inbound] = []
        for entry in _loads(raw).get("entry", []):
            for change in entry.get("changes", []):
                for m in change.get("value", {}).get("messages", []):
                    if m.get("type") != "text":
                        continue  # ignore media/status callbacks in this first version
                    out.append(
                        Inbound(
                            external_user_id=str(m.get("from", "")),
                            text=m.get("text", {}).get("body", ""),
                            message_id=str(m.get("id", "")),
                        )
                    )
        return out

    async def send(self, config: dict, to: str, text: str) -> None:
        phone_id = config.get("phone_number_id")
        token = config.get("access_token")
        if not phone_id or not token:
            log.warning("whatsapp_send_skipped: missing phone_number_id or access_token")
            return
        version = config.get("graph_version", GRAPH_VERSION)
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(
                f"{META_GRAPH}/{version}/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            response.raise_for_status()


class InstagramAdapter:
    name = "instagram"
    has_challenge = True
    verify_field = "verify_token"

    def routing_key(self, headers, raw: bytes) -> str | None:
        try:
            return str(_loads(raw)["entry"][0]["id"])
        except (KeyError, IndexError, TypeError):
            return None

    def challenge(self, params: dict, config: dict) -> str | None:
        return _meta_challenge(params, config)

    def verify(self, headers, raw: bytes, config: dict) -> bool:
        return _verify_meta(config, raw, headers.get("x-hub-signature-256"))

    def parse(self, raw: bytes) -> list[Inbound]:
        out: list[Inbound] = []
        for entry in _loads(raw).get("entry", []):
            for m in entry.get("messaging", []):
                message = m.get("message", {})
                text = message.get("text")
                if not text or message.get("is_echo"):
                    continue
                out.append(
                    Inbound(
                        external_user_id=str(m.get("sender", {}).get("id", "")),
                        text=text,
                        message_id=str(message.get("mid", "")),
                    )
                )
        return out

    async def send(self, config: dict, to: str, text: str) -> None:
        account_id = config.get("account_id")
        token = config.get("access_token")
        if not account_id or not token:
            log.warning("instagram_send_skipped: missing account_id or access_token")
            return
        version = config.get("graph_version", GRAPH_VERSION)
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(
                f"{INSTAGRAM_GRAPH}/{version}/{account_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"recipient": {"id": to}, "message": {"text": text}},
            )
            response.raise_for_status()


class TelegramAdapter:
    name = "telegram"
    has_challenge = False
    verify_field = "webhook_secret"

    def routing_key(self, headers, raw: bytes) -> str | None:
        # Telegram updates carry no account id; the bot is identified by the secret
        # token we set on setWebhook and that Telegram echoes on every delivery.
        return headers.get("x-telegram-bot-api-secret-token")

    def challenge(self, params: dict, config: dict) -> str | None:
        return None

    def verify(self, headers, raw: bytes, config: dict) -> bool:
        secret = config.get("webhook_secret") or ""
        provided = headers.get("x-telegram-bot-api-secret-token") or ""
        return bool(secret) and hmac.compare_digest(secret, provided)

    def parse(self, raw: bytes) -> list[Inbound]:
        message = _loads(raw).get("message") or {}
        text = message.get("text")
        if not text:
            return []
        return [
            Inbound(
                external_user_id=str(message.get("chat", {}).get("id", "")),
                text=text,
                message_id=str(message.get("message_id", "")),
            )
        ]

    async def send(self, config: dict, to: str, text: str) -> None:
        token = config.get("bot_token")
        if not token:
            log.warning("telegram_send_skipped: missing bot_token")
            return
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": to, "text": text},
            )
            response.raise_for_status()


ADAPTERS = {a.name: a for a in (WhatsAppAdapter(), InstagramAdapter(), TelegramAdapter())}
