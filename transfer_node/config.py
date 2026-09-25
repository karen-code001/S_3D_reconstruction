from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class ComputeTarget:
    node_id: str
    base_url: str
    weight: float = 1.0


class Settings(BaseSettings):
    node_id: str = "transfer-1"  ## (use a separate node_id per machine)
    public_url: str = "http://localhost:8101"
    callback_url: str = "http://transfer-1:8100"
    control_plane_url: str = "http://localhost:8000"
    internal_api_key: str = "change-me-in-production"
    upload_token_secret: str = "change-me-in-production"
    storage_root: Path = Path("./data/transfer")
    max_upload_bytes: int = 20 * 1024 * 1024 * 1024
    max_concurrent_uploads: int = 4
    heartbeat_interval_seconds: int = 10
    dispatch_interval_seconds: int = 5
    compute_nodes: str = ""
    cors_origins: str = "http://localhost:5173"

    model_config = SettingsConfigDict(env_prefix="TRANSFER_", env_file=".env", extra="ignore")

    @property
    def compute_targets(self) -> list[ComputeTarget]:
        targets: list[ComputeTarget] = []
        for entry in self.compute_nodes.split(","):
            if not entry.strip():
                continue
            parts = [part.strip() for part in entry.split("|")]
            if len(parts) not in (2, 3):
                raise ValueError("TRANSFER_COMPUTE_NODES entries must be node_id|url or node_id|url|weight")
            targets.append(ComputeTarget(parts[0], parts[1].rstrip("/"), float(parts[2]) if len(parts) == 3 else 1.0))
        return targets

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

