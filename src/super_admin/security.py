import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from .errors import AuthError

PASSWORD_HASH = PasswordHash.recommended()
# Verify unknown/disabled accounts too, so the password check doesn't reveal existence.
DUMMY_HASH = PASSWORD_HASH.hash(secrets.token_urlsafe(32))
ISSUER = "context-agent"
AUDIENCE = "super-admin"


def signing_key(settings) -> str:
    value = getattr(settings, "super_admin_jwt_secret", None)
    key = value.get_secret_value() if value else ""
    if not key.strip() or len(key.encode()) < 32:
        raise AuthError(503, "auth_not_configured", "Super-admin authentication is not configured.")
    return key


def validate_password(password: str) -> None:
    if not 15 <= len(password) <= 128 or not password.strip():
        raise ValueError("Password must contain 15 to 128 characters and cannot be blank.")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return PASSWORD_HASH.verify(password, password_hash)
    except (UnknownHashError, ValueError):
        return False


def create_token(account, settings) -> str:
    return issue_access_token(
        account.id, account.token_version, settings, role="super_admin", audience=AUDIENCE
    )


def issue_access_token(subject, version, settings, *, role: str, audience: str, **extra) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            **extra,
            "sub": str(subject),
            "role": role,
            "ver": version,
            "iss": ISSUER,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(minutes=settings.super_admin_token_minutes),
            "jti": secrets.token_hex(16),
        },
        signing_key(settings),
        algorithm="HS256",
    )


def decode_token(token: str, settings) -> tuple[UUID, int]:
    claims = decode_access_token(token, settings, role="super_admin", audience=AUDIENCE)
    return UUID(claims["sub"]), claims["ver"]


def decode_access_token(token: str, settings, *, role: str, audience: str) -> dict:
    key = signing_key(settings)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=audience,
            options={"require": ["sub", "role", "ver", "iss", "aud", "iat", "exp", "jti"]},
        )
        if claims["role"] != role or type(claims["ver"]) is not int:
            raise ValueError("Invalid role or token version")
        UUID(claims["sub"])
        return claims
    except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
        raise AuthError(401, "invalid_token", "Invalid or expired access token.") from None
