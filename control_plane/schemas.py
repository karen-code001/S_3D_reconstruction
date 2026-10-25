from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator

from common.status import TaskStatus
from common.plants import validate_plant_key, validate_yolo_ini


class TaskCreate(BaseModel):
    plant_type: str | None = None
    task_name: str | None = Field(default=None, max_length=80)
    group_name: str | None = Field(default=None, max_length=50)
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

    @field_validator("plant_type")
    @classmethod
    def validate_plant_type(cls, value: str | None) -> str | None:
        return validate_plant_key(value) if value is not None else None


class TaskCreated(BaseModel):
    plant_type: str | None = None
    yolo_ini: str | None = None
    segment: bool | None = None
    task_id: str
    status: TaskStatus
    upload_url: str
    upload_expires_at: datetime
    transfer_node_id: str


class TaskView(BaseModel):
    plant_type: str | None = None
    yolo_ini: str | None = None
    segment: bool | None = None
    task_name: str | None = None
    group_name: str | None = None
    task_id: str
    original_filename: str | None
    content_type: str | None
    file_expected_size_MB:float | None  # Convert bytes to MB
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
    uploaded_at: datetime | None = None
    completed_at: datetime | None


class PlantView(BaseModel):
    key: str
    name_zh: str
    name_en: str
    yolo_ini: str
    segment: bool
    enabled: bool
    sort_order: int


class PlantUpdate(BaseModel):
    name_zh: str | None = Field(default=None, min_length=1, max_length=50)
    name_en: str | None = Field(default=None, max_length=80)
    yolo_ini: str | None = None
    segment: bool | None = None
    enabled: bool | None = None
    sort_order: int | None = None

    @field_validator("name_zh", "name_en")
    @classmethod
    def strip_names(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("yolo_ini")
    @classmethod
    def check_yolo_ini(cls, value: str | None) -> str | None:
        return validate_yolo_ini(value.strip()) if value is not None else None


class PlantCreate(PlantUpdate):
    key: str
    name_zh: str = Field(min_length=1, max_length=50)
    name_en: str = Field(default="", max_length=80)
    yolo_ini: str
    segment: bool = True
    enabled: bool = True

    @field_validator("key")
    @classmethod
    def check_key(cls, value: str) -> str:
        return validate_plant_key(value.strip())


class YoloIniFile(BaseModel):
    name: str
    size: int


class ComputeYoloInis(BaseModel):
    node_id: str
    transfer_node_id: str
    reachable: bool
    files: list[YoloIniFile] = []
    error: str | None = None


class NodeHeartbeat(BaseModel):
    node_id: str = Field(min_length=1, max_length=100, examples=["example"])
    public_url: HttpUrl
    current_uploads: int = Field(default=0, ge=0)
    max_uploads: int = Field(default=4, ge=1)
    pending_tasks: int = Field(default=0, ge=0)
    network_usage_percent: float = Field(default=0, ge=0, le=100)
    disk_usage_percent: float = Field(default=0, ge=0, le=100)
    draining: bool = False


class TransferNodeStatus(BaseModel):
    node_id: str
    online: bool
    assignable: bool
    seconds_since_heartbeat: int
    current_uploads: int
    max_uploads: int
    disk_usage_percent: float


class ComputeNodeStatus(BaseModel):
    node_id: str
    transfer_node_id: str
    reachable: bool
    available: bool
    active_jobs: int | None = None
    capacity: int | None = None
    reason: str | None = None


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
