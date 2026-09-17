import re
import secrets
import unicodedata

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from context_agent.db import (
    ChannelAccount,
    Conversation,
    Document,
    SupportTicket,
    TenantSettings,
    Tool,
)
from context_agent.schemas import DomainError
from super_admin.security import PASSWORD_HASH, signing_key, validate_password

from .models import Business, BusinessAdmin
from .schemas import RESERVED_SLUGS


def slug_from_name(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:63].rstrip("-")
    if not slug:
        slug = "business"
    if len(slug) < 3 or slug in RESERVED_SLUGS:
        slug += "-business"
    return slug


async def slug_taken(session, slug: str) -> bool:
    if await session.scalar(select(Business.id).where(Business.slug == slug)):
        return True
    # Do not attach a newly provisioned owner to pre-existing legacy tenant data.
    for model in (Document, Conversation, ChannelAccount, SupportTicket, TenantSettings, Tool):
        if await session.scalar(select(model.tenant_id).where(model.tenant_id == slug).limit(1)):
            return True
    return False


async def suggest_slug(db, name: str) -> str:
    base = slug_from_name(name)
    async with db.transaction() as session:
        for suffix in range(1, 1001):
            candidate = base if suffix == 1 else f"{base[:58].rstrip('-')}-{suffix}"
            if not await slug_taken(session, candidate):
                return candidate
    raise DomainError(409, "slug_unavailable", "Please choose a different business subdomain.")


async def create_business(db, settings, payload):
    signing_key(settings)
    slug = payload.slug or await suggest_slug(db, payload.name)
    password = secrets.token_urlsafe(24)
    hashed = await run_in_threadpool(PASSWORD_HASH.hash, password)
    try:
        # Match legacy tenant-write locking while checking whether the slug is free.
        async with db.transaction(slug) as session:
            if await slug_taken(session, slug):
                raise DomainError(
                    409, "slug_taken", "This subdomain is already in use. Choose another slug."
                )
            business = Business(
                slug=slug,
                name=payload.name,
                description=payload.description,
                timezone=payload.timezone,
                plan=payload.plan,
            )
            session.add(business)
            await session.flush()
            from modules.models import BusinessModules

            session.add(BusinessModules(business_id=business.id))
            account = BusinessAdmin(business_id=business.id, username=slug, password_hash=hashed)
            session.add(account)
            await session.flush()
    except IntegrityError:
        raise DomainError(
            409, "slug_taken", "This subdomain is already in use. Choose another slug."
        ) from None
    return business, account.username, password


async def get_business(session, slug: str) -> Business:
    business = await session.scalar(select(Business).where(Business.slug == slug))
    if business is None:
        raise DomainError(404, "business_not_found", "Business not found.")
    return business


async def reset_business_password(db, username: str, password: str) -> None:
    from super_admin.service import normalize_username

    username = normalize_username(username)
    validate_password(password)
    hashed = await run_in_threadpool(PASSWORD_HASH.hash, password)
    async with db.transaction() as session:
        account = await session.scalar(
            select(BusinessAdmin).where(BusinessAdmin.username == username).with_for_update()
        )
        if account is None:
            raise ValueError("Business-admin account not found.")
        account.password_hash = hashed
        account.token_version += 1
        account.failed_login_attempts = 0
        account.locked_until = None
