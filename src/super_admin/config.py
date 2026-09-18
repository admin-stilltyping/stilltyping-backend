from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SuperAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Empty disables authentication until the operator configures a server-only key.
    super_admin_jwt_secret: SecretStr | None = None
    # Shared by business-admin and super-admin logins: two days from sign-in.
    super_admin_token_minutes: int = Field(default=2880, ge=1, le=2880)
    super_admin_login_max_attempts: int = Field(default=5, ge=1, le=20)
    super_admin_login_lock_seconds: int = Field(default=900, ge=1, le=86400)
