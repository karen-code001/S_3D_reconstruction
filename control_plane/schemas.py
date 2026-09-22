from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator

from common.status import TaskStatus


class TaskCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    content_type: str | None = Field(default=None, max_length=200)
    size: int | None = Field(default=None, gt=0)
    sha256: str | None = None

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value)):
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        return value.lower() if value else None


class TaskCreated(BaseModel):
    task_id: str
    status: TaskStatus
    upload_url: str
    upload_expires_at: datetime
    transfer_node_id: str


class TaskView(BaseModel):
    task_id: str
    status: TaskStatus
    progress: float
    current_stage: str
    transfer_node_id: str
    compute_node_id: str | None
    result_url: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class NodeHeartbeat(BaseModel):
    node_id: str = Field(min_length=1, max_length=100)
    public_url: HttpUrl
    current_uploads: int = Field(default=0, ge=0)
    max_uploads: int = Field(default=4, ge=1)
    pending_tasks: int = Field(default=0, ge=0)
    network_usage_percent: float = Field(default=0, ge=0, le=100)
    disk_usage_percent: float = Field(default=0, ge=0, le=100)
    draining: bool = False


class TaskStatusUpdate(BaseModel):
    node_id: str
    status: TaskStatus
    progress: float = Field(ge=0, le=100)
    current_stage: str = Field(min_length=1, max_length=100)
    compute_node_id: str | None = None
    actual_size: int | None = Field(default=None, ge=0)
    actual_sha256: str | None = None
    result_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None

