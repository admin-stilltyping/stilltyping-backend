from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from super_admin.config import SuperAdminSettings


class Settings(SuperAdminSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+asyncpg://agent:agent@localhost:5432/agent"
    qdrant_url: str = "http://localhost:6333"
    tenant_api_key: SecretStr | None = None
    qdrant_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
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
