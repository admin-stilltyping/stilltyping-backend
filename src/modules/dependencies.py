from typing import Annotated

from fastapi import Depends, Request, Response

from super_admin.businesses.auth import BusinessIdentity, require_business_admin
from super_admin.errors import AuthError
from super_admin.routes import no_cache


async def require_support_owner(
    request: Request,
    response: Response,
    identity: Annotated[BusinessIdentity, Depends(require_business_admin)],
):
    if identity.business.slug != request.path_params["tenant_id"]:
        raise AuthError(403, "tenant_access_denied", "You do not have access to this business.")
    no_cache(response)
    return identity
