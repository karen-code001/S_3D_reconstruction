from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TransferNode(Base):
    __tablename__ = "transfer_nodes"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    public_url: Mapped[str] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    draining: Mapped[bool] = mapped_column(Boolean, default=False)
    current_uploads: Mapped[int] = mapped_column(Integer, default=0)
    max_uploads: Mapped[int] = mapped_column(Integer, default=4)
    pending_tasks: Mapped[int] = mapped_column(Integer, default=0)
    network_usage_percent: Mapped[float] = mapped_column(Float, default=0.0)
    disk_usage_percent: Mapped[float] = mapped_column(Float, default=0.0)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TransferNodePause(Base):
    """Transfer nodes an admin paused; kept apart from transfer_nodes, which is cleared on shutdown."""

    __tablename__ = "transfer_node_pauses"

    node_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    paused_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plant(Base):
    __tablename__ = "plants"

    key: Mapped[str] = mapped_column(String(20), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(50))
    name_en: Mapped[str] = mapped_column(String(80), default="")
    yolo_ini: Mapped[str] = mapped_column(String(200))
    # False runs reconstruction only (RGB, no YOLO segmentation).
    segment: Mapped[bool] = mapped_column(Boolean, default=True)
    # Disabled crops are hidden from upload pickers but keep labelling their old tasks.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReconstructionTask(Base):
    __tablename__ = "reconstruction_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    plant_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Copied from the plants table at creation so later crop edits do not change submitted tasks.
    yolo_ini: Mapped[str | None] = mapped_column(String(200), nullable=True)
    segment: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Hidden from the 8088 task list by an admin (/ui/tasks.html); data and results are kept.
    hidden: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    task_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expected_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="CREATED", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    current_stage: Mapped[str] = mapped_column(String(100), default="waiting_for_upload")
    transfer_node_id: Mapped[str] = mapped_column(ForeignKey("transfer_nodes.id"), index=True)
    compute_node_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

