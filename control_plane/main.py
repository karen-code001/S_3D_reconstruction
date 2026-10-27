import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re
from uuid import uuid4
from urllib.parse import quote, unquote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, object_session

from common.security import create_upload_token
from common.plants import LEGACY_RECONSTRUCTION_ONLY, yolo_ini_for
from common.status import ALLOWED_TRANSITIONS, TERMINAL_STATUSES, TaskStatus

from .config import Settings, get_settings
from .database import Base, SessionLocal, engine, get_db, ensure_task_columns, seed_plants
from .load_balancer import NodeSnapshot, _as_utc, choose_transfer_node, is_assignable
from .models import Plant, ReconstructionTask, TaskHideRule, TransferNode, TransferNodePause, utcnow
from .schemas import (
    ComputeNodePath,
    ComputeNodeStatus,
    ComputeYoloInis,
    NodeHeartbeat,
    PlantCreate,
    PlantUpdate,
    PlantView,
    TaskCreate,
    TaskCreated,
    TaskDayCount,
    TaskHideRuleView,
    TaskStatusUpdate,
    TaskView,
    TaskVisibilityCount,
    TaskVisibilityRange,
    TaskVisibilityUpdate,
    TransferNodeStatus,
)

logger = logging.getLogger(__name__)
internal_api_key_header = APIKeyHeader(name="X-Internal-Key", auto_error=False)
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)
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
    ensure_task_columns()
    seed_plants()
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
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Key", "X-Admin-Key"],
)

webui_dir = Path(__file__).resolve().parent.parent / "web_UI"
if webui_dir.is_dir():
    app.mount("/ui", StaticFiles(directory=webui_dir, html=True), name="web_ui")


def require_internal_key(
    x_internal_key: str | None = Security(internal_api_key_header),
    config: Settings = Depends(get_settings),
) -> None:
    if x_internal_key != config.internal_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal key")


def require_admin_key(
    x_admin_key: str | None = Security(admin_key_header),
    config: Settings = Depends(get_settings),
) -> None:
    if x_admin_key != config.effective_admin_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin key")


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
    paused = _paused_node_ids(db)
    snapshots = [_snapshot(n, n.id in paused) for n in rows]
    selected = choose_transfer_node(snapshots, config.node_stale_after_seconds)
    if selected is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="no healthy transfer node")

    yolo_ini = segment = None
    if body.plant_type is not None:
        plant = db.get(Plant, body.plant_type)
        if plant is None or not plant.enabled:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unknown or disabled plant_type")
        yolo_ini, segment = plant.yolo_ini, plant.segment

    task_id = str(uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=config.upload_token_ttl_seconds)
    token = create_upload_token(
        task_id,
        selected.id,
        config.upload_token_secret,
        config.upload_token_ttl_seconds,
        expected_size=body.size,
        expected_sha256=body.sha256,
        plant_type=body.plant_type,
        yolo_ini=yolo_ini,
        segment=segment,
    )
    task = ReconstructionTask(
        id=task_id,
        original_filename=body.filename,
        plant_type=body.plant_type,
        yolo_ini=yolo_ini,
        segment=segment,
        task_name=body.task_name,
        group_name=body.group_name,
        hidden=_hidden_by_rule(db, utcnow()),
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
    print(f"Create_task: task_id={task_id}, token={token}. ",
          f"TRANSFER_NODE_ID={node.id}, PUBLIC_URL={node.public_url}. ",
          f"expected_file_name={task.original_filename}, content_type={task.content_type}, expected_size={task.expected_size}.");
    return TaskCreated(
        plant_type=body.plant_type,
        yolo_ini=yolo_ini,
        segment=segment,
        task_id=task_id,
        status=TaskStatus.CREATED,
        upload_url=f"{selected.public_url.rstrip('/')}/api/v1/uploads/{task_id}?token={token}",
        upload_expires_at=expires_at,
        transfer_node_id=selected.id,
    )


@app.get("/api/v1/tasks", response_model=list[TaskView])
def list_recent_tasks(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[TaskView]:
    tasks = db.scalars(
        select(ReconstructionTask)
        .where(or_(ReconstructionTask.hidden.is_(None), ReconstructionTask.hidden.is_(False)))
        .order_by(ReconstructionTask.created_at.desc())
        .limit(limit)
    ).all()
    return [_task_view(task) for task in tasks]


@app.get("/api/v1/tasks/{task_id}", response_model=TaskView)
def get_task(task_id: str, db: Session = Depends(get_db)) -> TaskView:
    """Hidden tasks are still returned (with hidden=true) so pages can tell 'hidden' from 'not found'."""
    task = db.get(ReconstructionTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return _task_view(task)


def _result_url(task: ReconstructionTask) -> str | None:
    file_relative_pathname = task.result_url
    if file_relative_pathname is None:
        return None
    db = object_session(task)
    if db is None:
        return None
    node = db.get(TransferNode, task.transfer_node_id)
    if node is None:
        return None
    # Keep only the relative path in the task; resolve the node on each query.
    return f"{node.public_url.rstrip('/')}{file_relative_pathname}"


def _tasks_in_range(db: Session, start: datetime, end: datetime) -> list[ReconstructionTask]:
    # Compared in Python: SQLite keeps naive UTC strings, so SQL comparisons with aware bounds are unreliable.
    start, end = _as_utc(start), _as_utc(end)
    return [task for task in db.scalars(select(ReconstructionTask)).all() if start <= _as_utc(task.created_at) < end]


@app.post(
    "/api/v1/admin/tasks/visibility/preview",
    response_model=TaskVisibilityCount,
    dependencies=[Depends(require_admin_key)],
)
def preview_task_visibility(body: TaskVisibilityRange, db: Session = Depends(get_db)) -> TaskVisibilityCount:
    tasks = _tasks_in_range(db, body.start, body.end)
    return TaskVisibilityCount(
        total=len(tasks),
        hidden=sum(bool(task.hidden) for task in tasks),
        future=_as_utc(body.end) > utcnow(),
        rules=len(_overlapping_rules(db, body.start, body.end)),
    )


def _active_rules(db: Session) -> list[TaskHideRule]:
    now = utcnow()
    return [rule for rule in db.scalars(select(TaskHideRule).order_by(TaskHideRule.start)).all() if _as_utc(rule.end) > now]


def _overlapping_rules(db: Session, start: datetime, end: datetime) -> list[TaskHideRule]:
    start, end = _as_utc(start), _as_utc(end)
    return [rule for rule in _active_rules(db) if _as_utc(rule.start) < end and _as_utc(rule.end) > start]


def _hidden_by_rule(db: Session, created_at: datetime) -> bool:
    """New tasks start hidden when an admin already hid a range covering their creation time."""
    created_at = _as_utc(created_at)
    return any(_as_utc(rule.start) <= created_at < _as_utc(rule.end) for rule in _active_rules(db))


@app.post(
    "/api/v1/admin/tasks/visibility",
    response_model=TaskVisibilityCount,
    dependencies=[Depends(require_admin_key)],
)
def set_task_visibility(body: TaskVisibilityUpdate, db: Session = Depends(get_db)) -> TaskVisibilityCount:
    """Hide or show every task created in [start, end) on the 8088 task list.

    Hiding a range that ends in the future also stores a rule, so tasks created later inside
    it start hidden; showing a range removes the active rules that overlap it.
    """
    tasks = _tasks_in_range(db, body.start, body.end)
    changed = [task for task in tasks if bool(task.hidden) != body.hidden]
    for task in changed:
        task.hidden = body.hidden
    future = _as_utc(body.end) > utcnow()
    if body.hidden and future:
        db.add(TaskHideRule(start=_as_utc(body.start), end=_as_utc(body.end)))
    removed = 0
    if not body.hidden:
        for rule in _overlapping_rules(db, body.start, body.end):
            db.delete(rule)
            removed += 1
    db.commit()
    logger.warning(
        "Task visibility changed by admin: hidden=%s start=%s end=%s matched=%s updated=%s rule_added=%s rules_removed=%s",
        body.hidden, body.start.isoformat(), body.end.isoformat(), len(tasks), len(changed),
        body.hidden and future, removed,
    )
    return TaskVisibilityCount(
        total=len(tasks),
        hidden=sum(bool(task.hidden) for task in tasks),
        updated=len(changed),
        future=future,
        rules=len(_overlapping_rules(db, body.start, body.end)),
    )


@app.get(
    "/api/v1/admin/tasks/hide-rules",
    response_model=list[TaskHideRuleView],
    dependencies=[Depends(require_admin_key)],
)
def list_hide_rules(db: Session = Depends(get_db)) -> list[TaskHideRuleView]:
    """Hide rules that still cover future submissions."""
    return [TaskHideRuleView(id=r.id, start=_as_utc(r.start), end=_as_utc(r.end)) for r in _active_rules(db)]


@app.delete(
    "/api/v1/admin/tasks/hide-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_key)],
)
def delete_hide_rule(rule_id: int, db: Session = Depends(get_db)) -> None:
    """Stop hiding new submissions in this range; tasks already hidden stay hidden."""
    rule = db.get(TaskHideRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="hide rule not found")
    db.delete(rule)
    db.commit()
    logger.warning("Task hide rule deleted by admin: id=%s start=%s end=%s", rule_id, rule.start, rule.end)


@app.get(
    "/api/v1/admin/tasks/daily",
    response_model=list[TaskDayCount],
    dependencies=[Depends(require_admin_key)],
)
def task_daily_counts(
    tz_offset_minutes: int = Query(default=480, ge=-840, le=840),
    db: Session = Depends(get_db),
) -> list[TaskDayCount]:
    """Tasks per local calendar day (UTC + tz_offset_minutes), newest day first."""
    offset = timedelta(minutes=tz_offset_minutes)
    days: dict[str, list[int]] = {}
    for task in db.scalars(select(ReconstructionTask)).all():
        day = (_as_utc(task.created_at) + offset).date().isoformat()
        counts = days.setdefault(day, [0, 0])
        counts[0] += 1
        counts[1] += bool(task.hidden)
    return [TaskDayCount(date=day, total=total, hidden=hidden) for day, (total, hidden) in sorted(days.items(), reverse=True)]


@app.get("/api/v1/plants", response_model=list[PlantView])
def list_plants(db: Session = Depends(get_db)) -> list[PlantView]:
    """All crops, disabled ones included so old tasks keep their names; pickers show enabled ones."""
    plants = db.scalars(select(Plant).order_by(Plant.sort_order, Plant.key)).all()
    return [_plant_view(plant) for plant in plants]


@app.get("/api/v1/admin/check", dependencies=[Depends(require_admin_key)])
def check_admin_key() -> dict[str, bool]:
    return {"ok": True}


@app.post(
    "/api/v1/admin/plants",
    response_model=PlantView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_key)],
)
def create_plant(body: PlantCreate, db: Session = Depends(get_db)) -> PlantView:
    if db.get(Plant, body.key) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="plant key already exists")
    sort_order = body.sort_order
    if sort_order is None:
        sort_order = (db.scalar(select(func.max(Plant.sort_order))) or 0) + 10
    plant = Plant(
        key=body.key,
        name_zh=body.name_zh,
        name_en=body.name_en,
        yolo_ini=body.yolo_ini,
        segment=body.segment,
        enabled=body.enabled,
        sort_order=sort_order,
    )
    db.add(plant)
    db.commit()
    logger.info("Plant created: key=%s yolo_ini=%s segment=%s", plant.key, plant.yolo_ini, plant.segment)
    return _plant_view(plant)


@app.put("/api/v1/admin/plants/{key}", response_model=PlantView, dependencies=[Depends(require_admin_key)])
def update_plant(key: str, body: PlantUpdate, db: Session = Depends(get_db)) -> PlantView:
    plant = db.get(Plant, key)
    if plant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plant not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(plant, field, value)
    db.commit()
    logger.info("Plant updated: key=%s yolo_ini=%s segment=%s enabled=%s", plant.key, plant.yolo_ini, plant.segment, plant.enabled)
    return _plant_view(plant)


@app.delete(
    "/api/v1/admin/plants/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_key)],
)
def delete_plant(key: str, db: Session = Depends(get_db)) -> None:
    plant = db.get(Plant, key)
    if plant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plant not found")
    # Tasks keep their own ini snapshot, but their crop name comes from this row.
    if db.scalar(select(ReconstructionTask.id).where(ReconstructionTask.plant_type == key).limit(1)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="plant is used by existing tasks; disable it instead")
    db.delete(plant)
    db.commit()
    logger.info("Plant deleted: key=%s", key)


@app.get(
    "/api/v1/admin/yolo-inis",
    response_model=list[ComputeYoloInis],
    dependencies=[Depends(require_admin_key)],
)
async def list_yolo_inis(config: Settings = Depends(get_settings)) -> list[ComputeYoloInis]:
    """Ini files present on each compute node, asked through the online transfer nodes."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.node_stale_after_seconds)
    with SessionLocal() as db:
        transfers = [
            (node.id, node.public_url)
            for node in db.scalars(select(TransferNode).order_by(TransferNode.id)).all()
            if node.last_heartbeat.replace(tzinfo=node.last_heartbeat.tzinfo or timezone.utc) >= cutoff
        ]

    async def query(client: httpx.AsyncClient, transfer_id: str, public_url: str) -> list[ComputeYoloInis]:
        try:
            response = await client.get(
                f"{public_url.rstrip('/')}/api/v1/compute-nodes/yolo-inis",
                headers={"X-Internal-Key": config.internal_api_key},
            )
            response.raise_for_status()
            nodes = response.json().get("nodes", [])
        except (httpx.HTTPError, ValueError, AttributeError):
            logger.warning("Failed to query compute ini files through TRANSFER_NODE_ID=%s", transfer_id)
            return []
        return [
            ComputeYoloInis(transfer_node_id=transfer_id, **item)
            for item in nodes
            if isinstance(item, dict) and item.get("node_id")
        ]

    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        groups = await asyncio.gather(*(query(client, tid, url) for tid, url in transfers))
    return [node for group in groups for node in group]


@app.get("/api/v1/transfer-nodes", response_model=list[TransferNodeStatus])
def list_transfer_node_status(
    db: Session = Depends(get_db),
    config: Settings = Depends(get_settings),
) -> list[TransferNodeStatus]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=config.node_stale_after_seconds)
    result = []
    paused = _paused_node_ids(db)
    for node in db.scalars(select(TransferNode).order_by(TransferNode.id)).all():
        snapshot = _snapshot(node, node.id in paused)
        last_heartbeat = snapshot.last_heartbeat
        if last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        result.append(
            TransferNodeStatus(
                node_id=node.id,
                online=last_heartbeat >= cutoff,
                assignable=is_assignable(snapshot, cutoff),
                seconds_since_heartbeat=max(0, int((now - last_heartbeat).total_seconds())),
                current_uploads=node.current_uploads,
                max_uploads=node.max_uploads,
                disk_usage_percent=round(node.disk_usage_percent, 1),
                paused=snapshot.paused,
            )
        )
    return result


@app.put(
    "/api/v1/admin/transfer-nodes/{node_id}/pause",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_key)],
)
def pause_transfer_node(node_id: str, db: Session = Depends(get_db)) -> None:
    """Stop assigning new uploads to a node; its accepted tasks still finish."""
    if db.get(TransferNode, node_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transfer node not registered")
    if db.get(TransferNodePause, node_id) is None:
        db.add(TransferNodePause(node_id=node_id))
        db.commit()
        logger.warning("Transfer node paused by admin: TRANSFER_NODE_ID=%s", node_id)


@app.delete(
    "/api/v1/admin/transfer-nodes/{node_id}/pause",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_key)],
)
def resume_transfer_node(node_id: str, db: Session = Depends(get_db)) -> None:
    pause = db.get(TransferNodePause, node_id)
    if pause is not None:
        db.delete(pause)
        db.commit()
        logger.warning("Transfer node resumed by admin: TRANSFER_NODE_ID=%s", node_id)


@app.get("/api/v1/compute-nodes", response_model=list[ComputeNodeStatus])
async def list_compute_node_status(config: Settings = Depends(get_settings)) -> list[ComputeNodeStatus]:
    # Compute nodes sit behind transfer nodes (often via SSH tunnels), so ask each online transfer node.
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.node_stale_after_seconds)
    with SessionLocal() as db:
        transfers = [
            (node.id, node.public_url)
            for node in db.scalars(select(TransferNode).order_by(TransferNode.id)).all()
            if node.last_heartbeat.replace(tzinfo=node.last_heartbeat.tzinfo or timezone.utc) >= cutoff
        ]

    async def query(client: httpx.AsyncClient, transfer_id: str, public_url: str) -> list[ComputeNodeStatus]:
        try:
            response = await client.get(
                f"{public_url.rstrip('/')}/api/v1/compute-nodes/availability",
                headers={"X-Internal-Key": config.internal_api_key},
            )
            response.raise_for_status()
            nodes = response.json().get("nodes", [])
        except (httpx.HTTPError, ValueError, AttributeError):
            logger.warning("Failed to query compute nodes through TRANSFER_NODE_ID=%s", transfer_id)
            return []
        return [
            ComputeNodeStatus(
                node_id=item["node_id"],
                transfer_node_id=transfer_id,
                reachable=bool(item.get("reachable")),
                available=bool(item.get("available")),
                active_jobs=item.get("active_jobs"),
                capacity=item.get("capacity"),
                reason=item.get("reason"),
            )
            for item in nodes
            if isinstance(item, dict) and item.get("node_id")
        ]

    async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
        groups = await asyncio.gather(*(query(client, tid, url) for tid, url in transfers))
    return merge_compute_reports([node for group in groups for node in group])


def merge_compute_reports(reports: list[ComputeNodeStatus]) -> list[ComputeNodeStatus]:
    """One row per compute node; transfer nodes configured with the same compute node id report the same machine."""
    by_node: dict[str, list[ComputeNodeStatus]] = {}
    for report in reports:
        by_node.setdefault(report.node_id, []).append(report)
    merged = []
    for node_id, seen in by_node.items():
        best = next((r for r in seen if r.available), None) or next((r for r in seen if r.reachable), seen[0])
        merged.append(
            best.model_copy(
                update={
                    "reachable": any(r.reachable for r in seen),
                    "available": any(r.available for r in seen),
                    "paths": [
                        ComputeNodePath(
                            transfer_node_id=r.transfer_node_id,
                            reachable=r.reachable,
                            available=r.available,
                            reason=r.reason,
                        )
                        for r in seen
                    ],
                }
            )
        )
    # Natural order, so Server-2 comes before Server-10.
    return sorted(merged, key=lambda n: [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", n.node_id)])


@app.post("/internal/nodes/heartbeat", dependencies=[Depends(require_internal_key)])
def node_heartbeat(body: NodeHeartbeat, db: Session = Depends(get_db)) -> dict[str, str]:
    public_url = str(body.public_url).rstrip("/")
    node = db.get(TransferNode, body.node_id)
    previous_heartbeat = node.last_heartbeat if node is not None else None
    is_new_node = node is None
    previous_public_url = node.public_url if node is not None else None
    if is_new_node:
        node = TransferNode(id=body.node_id, public_url=public_url)
        db.add(node)

    def apply_heartbeat(target: TransferNode) -> None:
        target.public_url = public_url
        target.active = True
        target.draining = body.draining
        target.current_uploads = body.current_uploads
        target.max_uploads = body.max_uploads
        target.pending_tasks = body.pending_tasks
        target.network_usage_percent = body.network_usage_percent
        target.disk_usage_percent = body.disk_usage_percent
        target.last_heartbeat = utcnow()

    apply_heartbeat(node)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not is_new_node:
            raise

        # Another heartbeat may have inserted the same node after our initial
        # lookup. Reload that row and apply this heartbeat as an update.
        node = db.get(TransferNode, body.node_id)
        if node is None:
            raise
        is_new_node = False
        previous_heartbeat = node.last_heartbeat
        previous_public_url = node.public_url
        apply_heartbeat(node)
        db.commit()

    if not is_new_node and previous_public_url != public_url:
        print(datetime.now());
        logger.warning(
            "Transfer node public URL changed: transfer_node_id=%s, old_public_url=%s, new_public_url=%s",
            body.node_id,
            previous_public_url,
            public_url,
        )

    if is_new_node:
        print(datetime.now());
        print(f"TRANSFER_NODE_ID={body.node_id}, heartbeat connection established:  PUBLIC_URL={body.public_url}");
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
    if body.status == TaskStatus.UPLOADED and task.uploaded_at is None:
        task.uploaded_at = task.updated_at
    if body.status in TERMINAL_STATUSES:
        task.completed_at = utcnow()
    if body.status == TaskStatus.RETRYING and old_status != TaskStatus.RETRYING:
        task.retry_count += 1
    db.commit()
    return _task_view(task)


def _paused_node_ids(db: Session) -> set[str]:
    return set(db.scalars(select(TransferNodePause.node_id)).all())


def _snapshot(node: TransferNode, paused: bool = False) -> NodeSnapshot:
    return NodeSnapshot(
        paused=paused,
        id=node.id,
        public_url=node.public_url,
        active=node.active,
        draining=node.draining,
        current_uploads=node.current_uploads,
        max_uploads=node.max_uploads,
        pending_tasks=node.pending_tasks,
        network_usage_percent=node.network_usage_percent,
        disk_usage_percent=node.disk_usage_percent,
        last_heartbeat=node.last_heartbeat,
    )


def _plant_view(plant: Plant) -> PlantView:
    return PlantView(
        key=plant.key,
        name_zh=plant.name_zh,
        name_en=plant.name_en,
        yolo_ini=plant.yolo_ini,
        segment=plant.segment,
        enabled=plant.enabled,
        sort_order=plant.sort_order,
    )


def _task_view(task: ReconstructionTask) -> TaskView:
    segment = task.segment
    if segment is None and task.plant_type:
        segment = task.plant_type not in LEGACY_RECONSTRUCTION_ONLY
    return TaskView(
        plant_type=task.plant_type,
        yolo_ini=task.yolo_ini or yolo_ini_for(task.plant_type),
        segment=segment,
        task_name=task.task_name,
        group_name=task.group_name,
        task_id=task.id,
        original_filename=task.original_filename,
        content_type=task.content_type,
        file_expected_size_MB= task.expected_size/1024/1024 if task.expected_size else task.expected_size,  # Convert bytes to MB
        status=TaskStatus(task.status),
        progress=task.progress,
        current_stage=task.current_stage,
        transfer_node_id=task.transfer_node_id,
        compute_node_id=task.compute_node_id,
        result_url=_result_url(task),
        error_code=task.error_code,
        error_message=task.error_message,
        created_at=_as_utc(task.created_at),
        updated_at=_as_utc(task.updated_at),
        uploaded_at=_as_utc(task.uploaded_at) if task.uploaded_at else None,
        completed_at=_as_utc(task.completed_at) if task.completed_at else None,
        hidden=bool(task.hidden),
    )


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "3D Reconstruction Control Plane",
        "status": "ok",
        "docs": "/docs",
        "health": "/health",
    }
