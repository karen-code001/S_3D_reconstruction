from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    node_id: str = "gpu-1"   # (use a separate node_id per machine)
    internal_api_key: str = "change-me-in-production"
    storage_root: Path = Path("./data/compute")
    capacity: int = 1
    max_upload_bytes: int = 20 * 1024 * 1024 * 1024
    callback_timeout_seconds: float = 30.0
    cors_origins: str = ""

    model_config = SettingsConfigDict(env_prefix="COMPUTE_", env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
