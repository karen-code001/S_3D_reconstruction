import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from common.security import create_upload_token
from common.status import ALLOWED_TRANSITIONS, TERMINAL_STATUSES, TaskStatus

from .config import Settings, get_settings
from .database import Base, SessionLocal, engine, get_db
from .load_balancer import NodeSnapshot, choose_transfer_node
from .models import ReconstructionTask, TransferNode, utcnow
from .schemas import NodeHeartbeat, TaskCreate, TaskCreated, TaskStatusUpdate, TaskView

logger = logging.getLogger(__name__)
internal_api_key_header = APIKeyHeader(name="X-Internal-Key", auto_error=False)
heartbeat_monitor_interval_seconds = 10
stale_node_ids: set[str] = set()


async def heartbeat_monitor_loop() -> None:
    """Log once when a registered node has been silent for more than one minute."""
    while True:
        db = SessionLocal()
        try:
            cutoff = utcnow() - timedelta(seconds=settings.node_stale_after_seconds)
            nodes = db.scalars(select(TransferNode)).all()
            for node in nodes:
                last_heartbeat = node.last_heartbeat
                if last_heartbeat.tzinfo is None:
                    last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
                if last_heartbeat < cutoff:
                    if node.id not in stale_node_ids:
                        print(datetime.now());
                        logger.warning(
                            "TRANSFER_NODE_ID=%s, transfer node heartbeat is abnormal: PUBLIC_URL=%s, last_heartbeat=%s",
                            node.id,
                            node.public_url,
                            last_heartbeat,
                        )
                        node.active = False
                        db.commit()
                        stale_node_ids.add(node.id)
        finally:
            db.close()
        await asyncio.sleep(heartbeat_monitor_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    heartbeat_monitor = asyncio.create_task(heartbeat_monitor_loop())
    try:
        yield
    finally:
        heartbeat_monitor.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_monitor

        db = SessionLocal()
        try:
            deleted = db.execute(delete(TransferNode)).rowcount or 0
            db.commit()
            stale_node_ids.clear()
            logger.info("Cleared registered transfer nodes during shutdown: count=%s", deleted)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Failed to clear registered transfer nodes during shutdown")
        finally:
            db.close()


app = FastAPI(title="3D Reconstruction Control Plane", version="0.1.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Key"],
)

webui_dir = Path(__file__).resolve().parent.parent / "webui"
if webui_dir.is_dir():
    app.mount("/ui", StaticFiles(directory=webui_dir, html=True), name="webui")


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
    print(datetime.now());
    print(f"Creat_task: task_id={task_id}, token={token}. TRANSFER_NODE_ID={node.id}, PUBLIC_URL={node.public_url}.");
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
    previous_heartbeat = node.last_heartbeat if node is not None else None
    if node is None:
        node = TransferNode(id=body.node_id, public_url=str(body.public_url).rstrip("/"))
        db.add(node)
        print(datetime.now());
        print(f"TRANSFER_NODE_ID={body.node_id}, heartbeat connection established:  PUBLIC_URL={body.public_url}");
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
    if previous_heartbeat is not None:
        if previous_heartbeat.tzinfo is None:
            previous_heartbeat = previous_heartbeat.replace(tzinfo=timezone.utc)
        heartbeat_gap = node.last_heartbeat - previous_heartbeat
    else:
        heartbeat_gap = None
    if heartbeat_gap is not None and heartbeat_gap > timedelta(minutes=1):
        logger.info(
            "Transfer node heartbeat connection recovered: TRANSFER_NODE_ID=%s "
            "previous_heartbeat=%s",
            node.id,
            previous_heartbeat,
        )
        print(datetime.now());
        print(f"TRANSFER_NODE_ID={node.id}, transfer node heartbeat connection recovered. PUBLIC_URL={node.public_url}");
        stale_node_ids.discard(node.id)
    logger.info("Heartbeat received: TRANSFER_NODE_ID=%s, PUBLIC_URL=%s", body.node_id, body.public_url)
    return {"status": "registered"}


@app.get("/internal/nodes", dependencies=[Depends(require_internal_key)])
def list_nodes(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    """List transfer nodes registered through the heartbeat endpoint."""
    nodes = db.scalars(select(TransferNode).order_by(TransferNode.id)).all()
    for node in nodes:
        print(f"Existing node: TRANSFER_NODE_ID={node.id}, PUBLIC_URL={node.public_url}");
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


@app.get(
    "/internal/tasks",
    response_model=list[TaskView],
    dependencies=[Depends(require_internal_key)],
)
def list_tasks(db: Session = Depends(get_db)) -> list[TaskView]:
    """List all reconstruction tasks, newest first."""
    tasks = db.scalars(
        select(ReconstructionTask).order_by(ReconstructionTask.created_at.desc())
    ).all()
    return [_task_view(task) for task in tasks]


@app.patch("/internal/tasks/{task_id}/status", dependencies=[Depends(require_internal_key)])
def update_task_status(
    task_id: str, body: TaskStatusUpdate, db: Session = Depends(get_db)
) -> TaskView:
    #print(datetime.now());
    #print(body);
    #print(f"task_id:{task_id}, status={body.status}, progress={body.progress}, transfer_node_id={body.node_id}, compute_node_id={body.compute_node_id}");
    if  "SUCCEEDED" == body.status:
        print(datetime.now());
        print(f"task_succeeded: task_id={task_id}, status={body.status}, progress={body.progress}, transfer_node_id={body.node_id}, compute_node_id={body.compute_node_id}, result_url={body.result_url}");

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
