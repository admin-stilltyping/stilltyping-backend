from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import AuthError
from .models import SuperAdmin
from .schemas import LoginInput, LoginOutput, SuperAdminProfile
from .security import create_token, decode_token
from .service import authenticate

router = APIRouter(prefix="/auth/super-admin", tags=["Super-admin authentication"])
bearer = HTTPBearer(auto_error=False)


async def require_super_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> SuperAdmin:
    if credentials is None:
        raise AuthError(401, "authentication_required", "A super-admin access token is required.")
    services = request.app.state.services
    account_id, version = decode_token(credentials.credentials, services["settings"])
    async with services["db"].transaction() as session:
        account = await session.get(SuperAdmin, account_id)
        if account is None or not account.is_active or account.token_version != version:
            raise AuthError(401, "invalid_token", "Invalid or expired access token.")
    return account


def no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/login", response_model=LoginOutput)
async def login(payload: LoginInput, request: Request, response: Response):
    services = request.app.state.services
    settings = services["settings"]
    account = await authenticate(
        services["db"], settings, payload.username, payload.password.get_secret_value()
    )
    no_cache(response)
    return LoginOutput(
        access_token=create_token(account, settings),
        expires_in=settings.super_admin_token_minutes * 60,
    )


@router.get("/me", response_model=SuperAdminProfile)
async def me(response: Response, account: Annotated[SuperAdmin, Depends(require_super_admin)]):
    no_cache(response)
    return SuperAdminProfile(id=account.id, username=account.username)
