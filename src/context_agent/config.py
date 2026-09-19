from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import make_url

from super_admin.config import SuperAdminSettings


class Settings(SuperAdminSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+asyncpg://agent:agent@localhost:5432/agent"
    database_pooler_mode: Literal["configured", "transaction"] = "configured"
    qdrant_url: str = "http://localhost:6333"
    tenant_api_key: SecretStr | None = None
    qdrant_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    integration_encryption_key: SecretStr | None = None
    chat_model: str = "gemini-3.5-flash"
    embedding_model: str = "gemini-embedding-2"
    embedding_dimensions: int = Field(default=3072, gt=0)
    index_prefix: str = "agent_gemini_embedding_2_3072"
    relevance_threshold: float = Field(default=0.60, ge=-1, le=1)
    candidate_limit: int = Field(default=30, ge=5, le=100)
    knowledge_limit: int = Field(default=4, ge=1, le=30)
    tool_limit: int = Field(default=5, ge=1, le=20)
    max_tool_rounds: int = Field(default=2, ge=1, le=10)
    history_limit: int = Field(default=10, ge=0, le=100)
    request_timeout: float = Field(default=180, gt=0)
    support_webhook_url: str | None = None
    support_webhook_token: SecretStr | None = None
    push_vapid_private_key: SecretStr | None = None
    push_vapid_subject: str = ""

    @model_validator(mode="after")
    def database_pooler(self):
        if self.database_pooler_mode == "transaction":
            url = make_url(self.database_url)
            if (
                url.drivername != "postgresql+asyncpg"
                or not (url.host or "").endswith(".pooler.supabase.com")
                or url.port not in (5432, 6543)
            ):
                raise ValueError("Transaction mode requires a Supabase asyncpg pooler URL.")
            # Switch only the pooler port. Preserve the exact database, host,
            # credentials and SSL options without reading/replacing the Vercel secret.
            self.database_url = url.set(port=6543).render_as_string(hide_password=False)
        return self
