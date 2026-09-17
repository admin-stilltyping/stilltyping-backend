from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from .errors import AuthError
from .models import SuperAdmin
from .schemas import Username
from .security import DUMMY_HASH, PASSWORD_HASH, signing_key, validate_password, verify_password


def normalize_username(username: str) -> str:
    return TypeAdapter(Username).validate_python(username).lower()


async def create_account(db, username: str, password: str) -> SuperAdmin:
    username = normalize_username(username)
    validate_password(password)
    hashed = await run_in_threadpool(PASSWORD_HASH.hash, password)
    account = SuperAdmin(username=username, password_hash=hashed)
    try:
        async with db.transaction() as session:
            session.add(account)
            await session.flush()
    except IntegrityError:
        raise ValueError("A super-admin with this username already exists.") from None
    return account


async def reset_password(db, username: str, password: str) -> None:
    username = normalize_username(username)
    validate_password(password)
    hashed = await run_in_threadpool(PASSWORD_HASH.hash, password)
    async with db.transaction() as session:
        account = await session.scalar(
            select(SuperAdmin).where(SuperAdmin.username == username).with_for_update()
        )
        if account is None:
            raise ValueError("Super-admin account not found.")
        account.password_hash = hashed
        account.token_version += 1
        account.failed_login_attempts = 0
        account.locked_until = None


async def authenticate(db, settings, username: str, password: str) -> SuperAdmin:
    signing_key(settings)
    # Bound the expensive hash input without stripping meaningful password whitespace.
    if not 1 <= len(password) <= 128:
        raise AuthError(401, "invalid_credentials", "Invalid username or password.")
    username = normalize_username(username)
    authenticated = False
    async with db.transaction() as session:
        # PostgreSQL serializes failures/reset/login for this account across workers.
        account = await session.scalar(
            select(SuperAdmin).where(SuperAdmin.username == username).with_for_update()
        )
        authenticated = await check_login_password(account, password, settings)
    # Raise after commit so failed-attempt counters are not rolled back.
    if not authenticated:
        raise AuthError(401, "invalid_credentials", "Invalid username or password.")
    return account


async def check_login_password(account, password: str, settings, *, allowed=True) -> bool:
    """Verify under an account row lock; callers commit failed counters before raising."""
    if not 1 <= len(password) <= 128:
        return False
    now = datetime.now(UTC)
    locked = False
    if account and account.locked_until:
        until = account.locked_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        locked = until > now
        if not locked:
            account.failed_login_attempts = 0
            account.locked_until = None
    eligible = account is not None and account.is_active and allowed and not locked
    hashed = account.password_hash if eligible else DUMMY_HASH
    verified = await run_in_threadpool(verify_password, password, hashed)
    if eligible and verified:
        account.failed_login_attempts = 0
        account.locked_until = None
        return True
    if eligible:
        account.failed_login_attempts += 1
        if account.failed_login_attempts >= settings.super_admin_login_max_attempts:
            account.locked_until = now + timedelta(seconds=settings.super_admin_login_lock_seconds)
    return False
