from fastapi.responses import JSONResponse


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


async def auth_error_response(request, exc: AuthError):
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
        headers=headers,
    )
