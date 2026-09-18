"""Temporary, time-bounded production account provisioning; remove after verification."""
import asyncio
import logging
import os
from datetime import UTC, datetime

from sqlalchemy import select, text

from .models import SuperAdmin

log = logging.getLogger(__name__)
USERNAME = "superadmin"
PASSWORD_HASH = '$argon2id$v=19$m=65536,t=3,p=4$FUhon6C5UDhKgiHW21aHLg$7ByL1+Z9ZyJaL4vwH0yuKS2YlbegwnA3KF7MaQcDalk'
EXPIRES_AT = datetime.fromisoformat('2026-09-18T10:40:58.124131+00:00')


async def provision_once(db):
    if os.environ.get("VERCEL_ENV") != "production" or datetime.now(UTC) >= EXPIRES_AT:
        return "skipped"
    try:
        async with asyncio.timeout(15), db.transaction() as session:
            if db.engine.dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(7812360941881)"))
            account = await session.scalar(select(SuperAdmin).where(SuperAdmin.username == USERNAME))
            if account is not None:
                log.info("One-time super-admin setup: account already exists; no changes.")
                return "exists"
            session.add(SuperAdmin(username=USERNAME, password_hash=PASSWORD_HASH))
            await session.flush()
        log.info("One-time super-admin setup: account created.")
        return "created"
    except Exception as exc:
        # Never log the exception text, URL, hash, or SQL parameter values.
        log.error("One-time super-admin setup failed (%s).", type(exc).__name__)
        return "failed"
