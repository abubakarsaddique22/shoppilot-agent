"""Settings (pydantic-settings). Env vars use the SHOP_ prefix, and the .env file is read too. For limits see configs/settings.dev.yaml."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SHOP_", extra="ignore")

    env: str = "dev"                      # dev | test | prod
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_to_console: bool = True

    database_url: str = "postgresql+psycopg://shop:shop@localhost:5432/shoppilot"
    llm_provider: str = "groq"          # groq | google
    llm_model: str = "set-in-env"
    llm_api_key: str = ""               # Groq key
    google_api_key: str = ""            # Gemini key (Google AI Studio)
    jwt_secret: str = "dev-only-change-me"

    auto_refund_limit_pkr: int = 3000
    manager_limit_pkr: int = 15000
    refund_window_days: int = 14
    late_threshold_days: int = 5
    max_tool_calls: int = 6
    max_retries: int = 2
    run_timeout_s: int = 45
    approval_ttl_hours: int = 48
    kb_min_score: float = 0.60
    embedding_model: str = "BAAI/bge-small-en-v1.5"  # fastembed model, 384 numbers per text; must match EMBEDDING_DIM in db/models.py
    s3_bucket: str = ""

    langsmith_tracing: bool = False
    langsmith_project: str = "shoppilot-dev"

    @property
    def psycopg_uri(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


settings = Settings()
