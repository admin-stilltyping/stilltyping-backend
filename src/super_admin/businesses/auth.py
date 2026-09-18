from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select

from super_admin.errors import AuthError
from super_admin.routes import bearer
from super_admin.security import decode_access_token, issue_access_token, signing_key
from super_admin.service import check_login_password, normalize_username

from .models import Business, BusinessAdmin

BUSINESS_AUDIENCE = "business-admin"


@dataclass
class BusinessIdentity:
    account: BusinessAdmin
    business: Business


async def login_business(db, settings, payload) -> BusinessIdentity:
    signing_key(settings)
    async with db.transaction() as session:
        business = await session.scalar(
            select(Business).where(Business.slug == payload.business_slug)
        )
        account = None
        if business is not None:
            account = await session.scalar(
                select(BusinessAdmin)
                .where(
                    BusinessAdmin.business_id == business.id,
                    BusinessAdmin.username == normalize_username(payload.username),
                )
                .with_for_update()
            )
        verified = await check_login_password(
            account,
            payload.password.get_secret_value(),
            settings,
            allowed=business is not None and business.status == "active",
        )
    if not verified:
        raise AuthError(
            401, "invalid_credentials", "Invalid username or password for this business."
        )
    return BusinessIdentity(account, business)


def create_business_token(identity: BusinessIdentity, settings) -> str:
    return issue_access_token(
        identity.account.id,
        identity.account.token_version,
        settings,
        role="admin",
        audience=BUSINESS_AUDIENCE,
        business_id=str(identity.business.id),
        business_slug=identity.business.slug,
    )


async def require_business_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> BusinessIdentity:
    if credentials is None:
        raise AuthError(401, "authentication_required", "Sign in to this business to continue.")
    services = request.app.state.services
    claims = decode_access_token(
        credentials.credentials, services["settings"], role="admin", audience=BUSINESS_AUDIENCE
    )
    async with services["db"].transaction() as session:
        account = await session.get(BusinessAdmin, UUID(claims["sub"]))
        business = await session.get(Business, account.business_id) if account else None
        if (
            account is None
            or not account.is_active
            or account.token_version != claims["ver"]
            or business is None
            or business.status != "active"
            or str(business.id) != claims.get("business_id")
            or business.slug != claims.get("business_slug")
        ):
            raise AuthError(401, "invalid_token", "Invalid or expired access token.")
    request.state.performance_owner_verified = True
    return BusinessIdentity(account, business)
