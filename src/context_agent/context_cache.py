"""Best-effort daytime context caching shared by serverless workers."""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import GeminiContextCache as Cache

log = logging.getLogger(__name__)
PROVIDER_TIMEOUT = 8
EXPIRY_MARGIN = timedelta(seconds=30)


def utc_now():
    return datetime.now(UTC)


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def daytime_expiry(now, timezone, start_hour=10, end_hour=22):
    local = now.astimezone(ZoneInfo(timezone or "Asia/Kolkata"))
    if not start_hour <= local.hour < end_hour:
        return None
    day = local.date() + timedelta(days=end_hour == 24)
    return datetime.combine(day, time(end_hour % 24), local.tzinfo).astimezone(UTC)


def provider_code(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return code
        exc = exc.__cause__
    return None


def cache_reference_error(exc):
    """Retry only rejected cache references, never ambiguous generation failures."""
    code = provider_code(exc)
    if code == 404:
        return True
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if code in (400, 403) and "cach" in str(exc).lower():
            return True
        exc = exc.__cause__
    return False


@dataclass(frozen=True)
class CacheRef:
    name: str
    expires_at: datetime

    def usable(self):
        return self.expires_at > utc_now() + EXPIRY_MARGIN


class DaytimeContextCache:
    def __init__(self, db, models, settings):
        self.db, self.models, self.settings = db, models, settings

    async def get(self, tenant, instruction, schemas, timezone):
        if not self.settings.gemini_cache_enabled:
            return None
        now = utc_now()
        try:
            expiry = daytime_expiry(
                now,
                timezone,
                self.settings.gemini_cache_start_hour,
                self.settings.gemini_cache_end_hour,
            )
            if expiry is None or expiry <= now + EXPIRY_MARGIN:
                return None
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "version": 1,
                        "tenant": tenant,
                        "model": self.settings.chat_model,
                        "credential": self.models.cache_credential_fingerprint,
                        "instruction": instruction,
                        "tools": schemas,
                        "timezone": timezone,
                        "hours": [
                            self.settings.gemini_cache_start_hour,
                            self.settings.gemini_cache_end_hour,
                        ],
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            return await self._get(tenant, fingerprint, instruction, schemas, now, expiry)
        except Exception as exc:
            # Never log prompts, keys, raw provider errors, or cache resource IDs.
            log.warning(
                "context_cache_unavailable tenant=%s error=%s provider_status=%s",
                tenant,
                type(exc).__name__,
                provider_code(exc),
            )
            return None

    async def _get(self, tenant, fingerprint, instruction, schemas, now, expiry):
        lease = uuid4()
        insert = pg_insert if self.db.engine.dialect.name == "postgresql" else sqlite_insert
        # A hit needs only a read, without an insert or tenant advisory lock.
        async with self.db.transaction() as session:
            row = await session.get(Cache, tenant)
        handled, reference = self._existing(row, fingerprint, now)
        if handled:
            return reference
        # Short transaction only: network work never holds a tenant/DB lock.
        async with self.db.transaction(tenant) as session:
            await session.execute(
                insert(Cache)
                .values(
                    tenant_id=tenant,
                    fingerprint="",
                )
                .on_conflict_do_nothing(index_elements=["tenant_id"])
            )
            row = await session.scalar(select(Cache).where(Cache.tenant_id == tenant))
            handled, reference = self._existing(row, fingerprint, now)
            if handled:
                return reference
            old_name = row.cache_name
            claimed = await session.execute(
                update(Cache)
                .execution_options(synchronize_session=False)
                .where(
                    Cache.tenant_id == tenant,
                    or_(Cache.lease_until.is_(None), Cache.lease_until <= now),
                    or_(
                        Cache.fingerprint != fingerprint,
                        Cache.cache_name.is_(None),
                        Cache.expires_at <= now + EXPIRY_MARGIN,
                    ),
                    or_(
                        Cache.fingerprint != fingerprint,
                        Cache.retry_after.is_(None),
                        Cache.retry_after <= now,
                    ),
                )
                .values(
                    fingerprint=fingerprint,
                    cache_name=None,
                    expires_at=None,
                    token_count=None,
                    lease_owner=lease,
                    lease_until=now + timedelta(seconds=45),
                    retry_after=None,
                )
            )
            if claimed.rowcount != 1:
                # Another worker is creating it; this request keeps working normally.
                return None

        created = None
        try:
            if old_name:
                await self._delete(old_name)
            async with asyncio.timeout(PROVIDER_TIMEOUT):
                created = await self.models.create_context_cache(instruction, schemas, expiry)
            if not created.name or not created.expire_time:
                raise ValueError("Provider cache omitted its identity or expiry")
            expires_at = min(aware(created.expire_time), expiry)
            count = getattr(getattr(created, "usage_metadata", None), "total_token_count", None)
            async with self.db.transaction(tenant) as session:
                saved = await session.execute(
                    update(Cache)
                    .execution_options(synchronize_session=False)
                    .where(
                        Cache.tenant_id == tenant,
                        Cache.lease_owner == lease,
                    )
                    .values(
                        cache_name=created.name,
                        expires_at=expires_at,
                        token_count=count,
                        lease_owner=None,
                        lease_until=None,
                        retry_after=None,
                    )
                )
            if saved.rowcount != 1 or expires_at <= utc_now() + EXPIRY_MARGIN:
                await self._delete(created.name)
                return None
            log.info("context_cache_created tenant=%s tokens=%s", tenant, count)
            return CacheRef(created.name, expires_at)
        except BaseException as exc:
            # Cache content cannot be padded to meet the provider's minimum. A
            # rejected configuration is skipped until its content changes or tomorrow.
            retry_after = (
                expiry if provider_code(exc) == 400 else min(expiry, now + timedelta(minutes=5))
            )
            try:
                async with self.db.transaction(tenant) as session:
                    await session.execute(
                        update(Cache)
                        .execution_options(synchronize_session=False)
                        .where(
                            Cache.tenant_id == tenant,
                            Cache.lease_owner == lease,
                        )
                        .values(lease_owner=None, lease_until=None, retry_after=retry_after)
                    )
            finally:
                if created is not None and created.name:
                    await self._delete(created.name)
            raise

    @staticmethod
    def _existing(row, fingerprint, now):
        if row is None:
            return False, None
        if row.fingerprint == fingerprint:
            if row.cache_name and row.expires_at and aware(row.expires_at) > now + EXPIRY_MARGIN:
                return True, CacheRef(row.cache_name, aware(row.expires_at))
            if row.retry_after and aware(row.retry_after) > now:
                return True, None
        if row.lease_until and aware(row.lease_until) > now:
            return True, None
        return False, None

    async def invalidate(self, tenant, name):
        try:
            async with self.db.transaction(tenant) as session:
                await session.execute(
                    update(Cache)
                    .execution_options(synchronize_session=False)
                    .where(
                        Cache.tenant_id == tenant,
                        Cache.cache_name == name,
                    )
                    .values(
                        cache_name=None,
                        expires_at=None,
                        retry_after=utc_now() + timedelta(minutes=5),
                    )
                )
            await self._delete(name)
        except Exception as exc:
            log.warning("context_cache_invalidate_failed error=%s", type(exc).__name__)

    async def _delete(self, name):
        try:
            async with asyncio.timeout(2):
                await self.models.delete_context_cache(name)
        except Exception:
            # A rotated/revoked key may no longer own the old cache; its fixed
            # provider expiry still bounds retention. It is never reused.
            pass
