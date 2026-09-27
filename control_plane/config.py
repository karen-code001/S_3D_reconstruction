from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "reconstruction-control-plane"
    database_url: str = "sqlite:///./control-plane.db"
    upload_token_secret: str = "change-me-in-production"
    internal_api_key: str = "change-me-in-production"
    upload_token_ttl_seconds: int = 3600
    node_stale_after_seconds: int = 60
    cors_origins: str = "null, http://localhost:8000, http://127.0.0.1:8000"

    model_config = SettingsConfigDict(env_prefix="CONTROL_", env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

