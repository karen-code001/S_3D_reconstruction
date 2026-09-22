from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.security import create_upload_token
from common.status import ALLOWED_TRANSITIONS, TERMINAL_STATUSES, TaskStatus

from .config import Settings, get_settings
from .database import Base, engine, get_db
from .load_balancer import NodeSnapshot, choose_transfer_node
from .models import ReconstructionTask, TransferNode, utcnow
from .schemas import NodeHeartbeat, TaskCreate, TaskCreated, TaskStatusUpdate, TaskView

logger = logging.getLogger(__name__)
internal_api_key_header = APIKeyHeader(name="X-Internal-Key", auto_error=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="3D Reconstruction Control Plane", version="0.1.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Key"],
)


def require_internal_key(
    x_internal_key: str | None = Security(internal_api_key_header),
    config: Settings = Depends(get_settings),
) -> None:
    if x_internal_key != config.internal_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal key")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.post("/api/v1/tasks", response_model=TaskCreated, status_code=status.HTTP_201_CREATED)
def create_task(
    body: TaskCreate,
    db: Session = Depends(get_db),
    config: Settings = Depends(get_settings),
) -> TaskCreated:
    # Row locks prevent concurrent API workers from over-allocating the same node.
    # SQLite ignores this clause in local development; PostgreSQL honors it.
    rows = db.scalars(select(TransferNode).with_for_update()).all()
    snapshots = [
        NodeSnapshot(
            id=n.id,
            public_url=n.public_url,
            active=n.active,
            draining=n.draining,
            current_uploads=n.current_uploads,
            max_uploads=n.max_uploads,
            pending_tasks=n.pending_tasks,
            network_usage_percent=n.network_usage_percent,
            disk_usage_percent=n.disk_usage_percent,
            last_heartbeat=n.last_heartbeat,
        )
        for n in rows
    ]
    selected = choose_transfer_node(snapshots, config.node_stale_after_seconds)
    if selected is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="no healthy transfer node")

    task_id = str(uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=config.upload_token_ttl_seconds)
    token = create_upload_token(
        task_id,
        selected.id,
        config.upload_token_secret,
        config.upload_token_ttl_seconds,
        expected_size=body.size,
        expected_sha256=body.sha256,
    )
    task = ReconstructionTask(
        id=task_id,
        original_filename=body.filename,
        content_type=body.content_type,
        expected_size=body.size,
        expected_sha256=body.sha256,
        transfer_node_id=selected.id,
        status=TaskStatus.CREATED.value,
    )
    node = db.get(TransferNode, selected.id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="transfer node disappeared")
    node.current_uploads += 1
    db.add(task)
    db.commit()
    return TaskCreated(
        task_id=task_id,
        status=TaskStatus.CREATED,
        upload_url=f"{selected.public_url.rstrip('/')}/api/v1/uploads/{task_id}?token={token}",
        upload_expires_at=expires_at,
        transfer_node_id=selected.id,
    )


@app.get("/api/v1/tasks/{task_id}", response_model=TaskView)
def get_task(task_id: str, db: Session = Depends(get_db)) -> TaskView:
    task = db.get(ReconstructionTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return _task_view(task)


@app.post("/internal/nodes/heartbeat", dependencies=[Depends(require_internal_key)])
def node_heartbeat(body: NodeHeartbeat, db: Session = Depends(get_db)) -> dict[str, str]:
    node = db.get(TransferNode, body.node_id)
    if node is None:
        node = TransferNode(id=body.node_id, public_url=str(body.public_url).rstrip("/"))
        db.add(node)
    node.public_url = str(body.public_url).rstrip("/")
    node.active = True
    node.draining = body.draining
    node.current_uploads = body.current_uploads
    node.max_uploads = body.max_uploads
    node.pending_tasks = body.pending_tasks
    node.network_usage_percent = body.network_usage_percent
    node.disk_usage_percent = body.disk_usage_percent
    node.last_heartbeat = utcnow()
    db.commit()
    logger.info("Heartbeat received: TRANSFER_NODE_ID=%s", body.node_id)
    return {"status": "registered"}


@app.get("/internal/nodes", dependencies=[Depends(require_internal_key)])
def list_nodes(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    """List transfer nodes registered through the heartbeat endpoint."""
    nodes = db.scalars(select(TransferNode).order_by(TransferNode.id)).all()
    return [
        {
            "node_id": node.id,
            "public_url": node.public_url,
            "active": node.active,
            "draining": node.draining,
            "current_uploads": node.current_uploads,
            "max_uploads": node.max_uploads,
            "pending_tasks": node.pending_tasks,
            "network_usage_percent": node.network_usage_percent,
            "disk_usage_percent": node.disk_usage_percent,
            "last_heartbeat": node.last_heartbeat,
            "created_at": node.created_at,
        }
        for node in nodes
    ]


@app.patch("/internal/tasks/{task_id}/status", dependencies=[Depends(require_internal_key)])
def update_task_status(
    task_id: str, body: TaskStatusUpdate, db: Session = Depends(get_db)
) -> TaskView:
    task = db.get(ReconstructionTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    if task.transfer_node_id != body.node_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="task belongs to another transfer node")

    old_status = TaskStatus(task.status)
    if body.status != old_status and body.status not in ALLOWED_TRANSITIONS[old_status]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"invalid transition: {old_status.value} -> {body.status.value}",
        )
    task.status = body.status.value
    task.progress = max(task.progress, body.progress) if body.status not in {TaskStatus.RETRYING, TaskStatus.FAILED} else body.progress
    task.current_stage = body.current_stage
    task.compute_node_id = body.compute_node_id or task.compute_node_id
    task.actual_size = body.actual_size if body.actual_size is not None else task.actual_size
    task.actual_sha256 = body.actual_sha256 or task.actual_sha256
    task.result_url = body.result_url or task.result_url
    task.error_code = body.error_code
    task.error_message = body.error_message
    task.version += 1
    task.updated_at = utcnow()
    if body.status in TERMINAL_STATUSES:
        task.completed_at = utcnow()
    if body.status == TaskStatus.RETRYING and old_status != TaskStatus.RETRYING:
        task.retry_count += 1
    db.commit()
    return _task_view(task)


def _task_view(task: ReconstructionTask) -> TaskView:
    return TaskView(
        task_id=task.id,
        status=TaskStatus(task.status),
        progress=task.progress,
        current_stage=task.current_stage,
        transfer_node_id=task.transfer_node_id,
        compute_node_id=task.compute_node_id,
        result_url=task.result_url,
        error_code=task.error_code,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
        completed_at=task.completed_at,
    )


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "3D Reconstruction Control Plane",
        "status": "ok",
        "docs": "/docs",
        "health": "/health",
    }
