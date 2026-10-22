import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from datetime import datetime
from urllib.parse import parse_qs

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from common.security import TokenError, verify_upload_token
from common.status import TaskStatus

from .clients import choose_compute_node, query_compute_node_availability, report_status, send_job
from .config import Settings, get_settings
from .storage import UploadTooLarge, store_upload, task_source
from .tunnels import TunnelSupervisor


settings = get_settings()
active_uploads = 0
upload_lock = asyncio.Lock()
upload_completion_lock = asyncio.Lock()
inflight_dispatches: set[str] = set()
logger = logging.getLogger(__name__)
heartbeat_connected = False


async def heartbeat_loop(app: FastAPI) -> None:
    global heartbeat_connected
    while True:
        try:
            usage = shutil.disk_usage(settings.storage_root)
            pending = sum(
                1
                for directory in (settings.storage_root / "inbox").iterdir()
                if directory.is_dir() and not (directory / ".dispatched").exists()
            )
            response = await app.state.client.post(
                f"{settings.control_plane_url.rstrip('/')}/internal/nodes/heartbeat",
                json={
                    "node_id": settings.node_id,
                    "public_url": settings.public_url,
                    "current_uploads": active_uploads,
                    "max_uploads": settings.max_concurrent_uploads,
                    "pending_tasks": pending,
                    "network_usage_percent": 0,
                    "disk_usage_percent": usage.used / usage.total * 100,
                    "draining": False,
                },
                headers={"X-Internal-Key": settings.internal_api_key},
            )
            response.raise_for_status()
            if not heartbeat_connected:
                logger.info(
                    "Heartbeat connection established: TRANSFER_NODE_ID=%s CONTROL_PLANE_URL=%s",
                    settings.node_id,
                    settings.control_plane_url,
                )
                print(datetime.now());
                print(f"TRANSFER_NODE_ID={settings.node_id}, heartbeat connection established: CONTROL_PLANE_URL={settings.control_plane_url}");
                heartbeat_connected = True
        except (httpx.HTTPError, OSError) as exc:
            print(datetime.now());
            logger.warning(
                "TRANSFER_NODE_ID=%s, heartbeat failed: CONTROL_PLANE_URL=%s ERROR=%s",
                settings.node_id,
                settings.control_plane_url,
                exc,
            )
            heartbeat_connected = False
        await asyncio.sleep(settings.heartbeat_interval_seconds)


async def dispatch_loop(app: FastAPI) -> None:
    while True:
        app.state.dispatch_wakeup.clear()
        await dispatch_pending(app)
        try:
            await asyncio.wait_for(
                app.state.dispatch_wakeup.wait(), timeout=settings.dispatch_interval_seconds
            )
        except asyncio.TimeoutError:
            pass


def ready_order(task_dir: Path) -> tuple[int, str]:
    marker = task_dir / ".ready"
    timestamp = marker.read_text(encoding="utf-8").strip()
    # Older deployments created empty markers; preserve their ready time on restart.
    ready_at = int(timestamp) if timestamp else marker.stat().st_mtime_ns
    return ready_at, task_dir.name


async def dispatch_pending(app: FastAPI) -> None:
    """Only this worker dispatches; an unaccepted head must not be overtaken."""
    inbox = settings.storage_root / "inbox"
    pending = sorted(
        (
            task_dir for task_dir in inbox.iterdir()
            if task_dir.is_dir()
            and (task_dir / ".ready").exists()
            and not (task_dir / ".dispatched").exists()
        ),
        key=ready_order,
    )
    for task_dir in pending:
        source = task_source(task_dir)
        if source is None:
            logger.warning("Queue head has no source: task_id=%s", task_dir.name)
            break
        await dispatch_task(app, task_dir.name, source)
        if not (task_dir / ".dispatched").exists():
            break


async def dispatch_task(app: FastAPI, task_id: str, source: Path) -> None:
    if task_id in inflight_dispatches or (source.parent / ".dispatched").exists():
        return
    inflight_dispatches.add(task_id)
    try:
        target = await choose_compute_node(app.state.client, settings.compute_targets)
        print(f"compute target: {target}");
        if target is None:
            queued_marker = source.parent / ".queued_reported"
            if not queued_marker.exists():
                await report_status(app.state.client, settings, task_id, TaskStatus.QUEUED, 15, "waiting_for_compute")
                queued_marker.touch()
            return
        await report_status(
            app.state.client,
            settings,
            task_id,
            TaskStatus.DISPATCHING,
            18,
            "dispatching_to_compute",
            compute_node_id=target.node_id,
        )
        await report_status(
            app.state.client,
            settings,
            task_id,
            TaskStatus.TRANSFERRING,
            20,
            "transferring_to_compute",
            compute_node_id=target.node_id,
        )
        await send_job(app.state.client, settings, target, task_id, source)
        (source.parent / ".dispatched").touch()
        await report_status(
            app.state.client,
            settings,
            task_id,
            TaskStatus.PROCESSING,
            25,
            "compute_accepted",
            compute_node_id=target.node_id,
        )
    except (httpx.HTTPError, OSError, ValueError) as exc:
        with suppress(httpx.HTTPError):
            await report_status(
                app.state.client,
                settings,
                task_id,
                TaskStatus.RETRYING,
                15,
                "dispatch_retry",
                error_code="COMPUTE_DISPATCH_FAILED",
                error_message=str(exc)[:1000],
            )
    finally:
        inflight_dispatches.discard(task_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    (settings.storage_root / "inbox").mkdir(parents=True, exist_ok=True)
    (settings.storage_root / "results").mkdir(parents=True, exist_ok=True)
    tunnels = TunnelSupervisor(settings.ssh_tunnel_hosts, settings.ssh_tunnel_retry_seconds)
    tunnels.start()
    # Internal traffic only; a Windows system proxy (e.g. Clash) must not intercept 127.0.0.1 tunnels.
    app.state.client = httpx.AsyncClient(timeout=10, trust_env=False)
    app.state.dispatch_wakeup = asyncio.Event()
    heartbeat = asyncio.create_task(heartbeat_loop(app))
    dispatcher = asyncio.create_task(dispatch_loop(app))
    yield
    for worker in (heartbeat, dispatcher):
        worker.cancel()
    for worker in (heartbeat, dispatcher):
        with suppress(asyncio.CancelledError):
            await worker
    await app.state.client.aclose()
    await asyncio.to_thread(tunnels.stop)


app = FastAPI(title="3D Reconstruction Transfer Node", version="0.1.0", lifespan=lifespan)


class UploadAuthMiddleware:
    """Authenticate and reserve upload capacity before reading the request body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        prefix = "/api/v1/uploads/"
        if not path.startswith(prefix):
            await self.app(scope, receive, send)
            return

        task_id = path[len(prefix) :].split("/", 1)[0]
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        token = query.get("token", [None])[0]
        error_status = None
        error_detail = None
        if not task_id or not token:
            error_status, error_detail = 401, "upload token is required"
        else:
            try:
                claims = verify_upload_token(token, settings.upload_token_secret)
                if claims.task_id != task_id or claims.node_id != settings.node_id:
                    error_status, error_detail = 403, "token does not match task or node"
            except TokenError as exc:
                error_status, error_detail = 401, str(exc)

        if error_status is not None:
            body = json.dumps({"detail": error_detail}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": error_status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        global active_uploads
        async with upload_lock:
            capacity_available = active_uploads < settings.max_concurrent_uploads
            if capacity_available:
                active_uploads += 1

        if not capacity_available:
            body = json.dumps({"detail": "upload capacity reached"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status.HTTP_429_TOO_MANY_REQUESTS,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        try:
            await self.app(scope, receive, send)
        finally:
            async with upload_lock:
                active_uploads -= 1


app.add_middleware(UploadAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Key"],
)


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    if x_internal_key != settings.internal_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal key")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "node_id": settings.node_id,
        "active_uploads": active_uploads,
        "max_uploads": settings.max_concurrent_uploads,
    }


@app.get(
    "/api/v1/compute-nodes/availability",
    dependencies=[Depends(require_internal_key)],
)
async def compute_nodes_availability() -> dict[str, object]:
    """Return whether at least one configured compute node can accept a task."""
    nodes = await query_compute_node_availability(app.state.client, settings.compute_targets)
    return {
        "available": any(node.available for node in nodes),
        "nodes": [node.to_dict() for node in nodes],
    }


@app.post("/api/v1/uploads/{task_id}", status_code=status.HTTP_202_ACCEPTED)
async def upload_video(
    task_id: str,
    background_tasks: BackgroundTasks,
    token: str = Query(min_length=10),
    file: UploadFile = File(...),
) -> dict[str, object]:
    try:
        claims = verify_upload_token(token, settings.upload_token_secret)
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    if claims.task_id != task_id or claims.node_id != settings.node_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="token does not match task or node")
    task_dir = settings.storage_root / "inbox" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    reservation = task_dir / ".uploading"
    try:
        reservation.touch(exist_ok=False)
    except FileExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this task is already uploading") from exc
    if (task_dir / ".ready").exists():
        reservation.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this task was already uploaded")
    try:
        await report_status(app.state.client, settings, task_id, TaskStatus.UPLOADING, 1, "receiving_video")
        stored = await store_upload(file, task_dir, settings.max_upload_bytes)
        if claims.expected_size is not None and stored.size != claims.expected_size:
            stored.path.unlink(missing_ok=True)
            await report_status(
                app.state.client,
                settings,
                task_id,
                TaskStatus.FAILED,
                0,
                "upload_validation_failed",
                error_code="SIZE_MISMATCH",
                error_message=f"expected {claims.expected_size} bytes, received {stored.size}",
            )
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="uploaded size mismatch")
        if claims.expected_sha256 is not None and stored.sha256.lower() != claims.expected_sha256.lower():
            stored.path.unlink(missing_ok=True)
            await report_status(
                app.state.client,
                settings,
                task_id,
                TaskStatus.FAILED,
                0,
                "upload_validation_failed",
                error_code="HASH_MISMATCH",
                error_message="uploaded SHA-256 does not match the declared value",
            )
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="uploaded SHA-256 mismatch")

        # Persist signed task metadata before making the upload dispatchable.
        metadata = stored.path.parent / ".task.json"
        temporary = stored.path.parent / ".task.json.tmp"
        temporary.write_text(json.dumps({"plant_type": claims.plant_type}), encoding="utf-8")
        temporary.replace(metadata)
        
        background_tasks.add_task(dispatch_task, app, task_id, stored.path)
        verified_at = time.time_ns()
        # Keep slow status requests from letting a later verified upload enqueue first.
        async with upload_completion_lock:
            await report_status(
                app.state.client,
                settings,
                task_id,
                TaskStatus.UPLOADED,
                15,
                "upload_verified",
                actual_size=stored.size,
                actual_sha256=stored.sha256,
            )
            (stored.path.parent / ".ready").write_text(str(verified_at), encoding="utf-8")
        app.state.dispatch_wakeup.set()

        return {"task_id": task_id, "status": TaskStatus.UPLOADED, "size": stored.size, "sha256": stored.sha256}
    except UploadTooLarge as exc:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="control plane unavailable") from exc
    finally:
        reservation.unlink(missing_ok=True)


@app.post(
    "/internal/results/{task_id}",
    dependencies=[Depends(require_internal_key)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_result(
    task_id: str,
    file: UploadFile = File(...),
    final: bool = Query(default=True),
) -> dict[str, object]:
    destination_dir = settings.storage_root / "results" / task_id
    complete_marker = destination_dir / ".complete"
    if complete_marker.exists():
        await file.close()
        return {"task_id": task_id, "received": file.filename or "result", "final": True, "duplicate": True}
    await report_status(
        app.state.client,
        settings,
        task_id,
        TaskStatus.RESULT_TRANSFERRING,
        95,
        "receiving_result",
    )
    stored = await store_upload(file, destination_dir, settings.max_upload_bytes)
    result_path = destination_dir / (file.filename or stored.path.name)
    if result_path != stored.path:
        stored.path.replace(result_path)
    if final:
        await report_status(
            app.state.client,
            settings,
            task_id,
            TaskStatus.SUCCEEDED,
            100,
            "completed",
            result_url=f"{settings.public_url.rstrip('/')}/api/v1/results/{task_id}/{result_path.name}",
        )
        complete_marker.touch()
    print(datetime.now());
    print(f"receive_result: task_id:{task_id}, result_name: {result_path.name}, final:{final}");
    return {"task_id": task_id, "received": result_path.name, "final": final}


class ComputeProgress(BaseModel):
    status: TaskStatus
    progress: float = Field(ge=25, le=94)
    stage: str = Field(min_length=1, max_length=100)
    compute_node_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@app.post("/internal/progress/{task_id}", dependencies=[Depends(require_internal_key)])
async def relay_compute_progress(
    task_id: str,
    body: ComputeProgress,
    request: Request,
) -> dict[str, object]:
    if body.status not in {
        TaskStatus.PROCESSING,
        TaskStatus.PACKAGING,
        TaskStatus.RETRYING,
        TaskStatus.FAILED,
    }:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported compute status")
    # compute_node_id = settings.compute_node_id_for_host(request.client.host if request.client else None)
    # if compute_node_id is None:
    #     logger.warning(
    #         "Could not map compute progress source host to TRANSFER_COMPUTE_NODES: task_id=%s host=%s",
    #         task_id,
    #         request.client.host if request.client else None,
    #     )
    progress_fields = {
        "error_code": body.error_code,
        "error_message": body.error_message,
    }
    # if compute_node_id is not None:
    #     progress_fields["compute_node_id"] = compute_node_id
    await report_status(
        app.state.client,
        settings,
        task_id,
        body.status,
        body.progress,
        body.stage,
        **progress_fields,
    )
    return {"task_id": task_id, "accepted": True}


# A task's result file is written once, so browsers may keep it and skip re-downloading on reload.
RESULT_CACHE_CONTROL = "private, max-age=31536000, immutable"


@app.get("/api/v1/results/{task_id}/{filename}")
def download_result(task_id: str, filename: str, if_none_match: Annotated[str | None, Header()] = None):
    # Add user ownership checks or a signed download token before exposing this route publicly.
    path = (settings.storage_root / "results" / task_id / Path(filename).name).resolve()
    result_root = (settings.storage_root / "results").resolve()
    if result_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="result not found")
    response = FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
        headers={"Cache-Control": RESULT_CACHE_CONTROL},
        stat_result=path.stat(),
    )
    etag = response.headers["etag"]
    # Starlette's FileResponse ignores If-None-Match, so answer revalidations here instead of resending the file.
    if if_none_match and (if_none_match.strip() == "*" or etag in {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag, "Cache-Control": RESULT_CACHE_CONTROL})
    return response


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "3D Reconstruction Transfer Node",
        "status": "ok",
        "docs": "/docs",
        "health": "/health",
        "compute_nodes_availability": "/api/v1/compute-nodes/availability",
    }

