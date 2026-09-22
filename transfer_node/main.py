import asyncio
import shutil
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from common.security import TokenError, verify_upload_token
from common.status import TaskStatus

from .clients import choose_compute_node, report_status, send_job
from .config import Settings, get_settings
from .storage import UploadTooLarge, store_upload, task_source


settings = get_settings()
active_uploads = 0
upload_lock = asyncio.Lock()
inflight_dispatches: set[str] = set()


async def heartbeat_loop(app: FastAPI) -> None:
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
        except (httpx.HTTPError, OSError):
            pass
        await asyncio.sleep(settings.heartbeat_interval_seconds)


async def dispatch_loop(app: FastAPI) -> None:
    while True:
        inbox = settings.storage_root / "inbox"
        for task_dir in inbox.iterdir():
            if (
                not task_dir.is_dir()
                or not (task_dir / ".ready").exists()
                or (task_dir / ".dispatched").exists()
                or task_dir.name in inflight_dispatches
            ):
                continue
            source = task_source(task_dir)
            if source is not None:
                asyncio.create_task(dispatch_task(app, task_dir.name, source))
        await asyncio.sleep(settings.dispatch_interval_seconds)


async def dispatch_task(app: FastAPI, task_id: str, source: Path) -> None:
    if task_id in inflight_dispatches or (source.parent / ".dispatched").exists():
        return
    inflight_dispatches.add(task_id)
    try:
        target = await choose_compute_node(app.state.client, settings.compute_targets)
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
    except (httpx.HTTPError, OSError) as exc:
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
    app.state.client = httpx.AsyncClient(timeout=10)
    heartbeat = asyncio.create_task(heartbeat_loop(app))
    dispatcher = asyncio.create_task(dispatch_loop(app))
    yield
    for worker in (heartbeat, dispatcher):
        worker.cancel()
    for worker in (heartbeat, dispatcher):
        with suppress(asyncio.CancelledError):
            await worker
    await app.state.client.aclose()


app = FastAPI(title="3D Reconstruction Transfer Node", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS"],
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


@app.post("/api/v1/uploads/{task_id}", status_code=status.HTTP_202_ACCEPTED)
async def upload_video(
    task_id: str,
    background_tasks: BackgroundTasks,
    token: str = Query(min_length=10),
    file: UploadFile = File(...),
) -> dict[str, object]:
    global active_uploads
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
    async with upload_lock:
        if active_uploads >= settings.max_concurrent_uploads:
            reservation.unlink(missing_ok=True)
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="upload capacity reached")
        active_uploads += 1
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
        (stored.path.parent / ".ready").touch()
        background_tasks.add_task(dispatch_task, app, task_id, stored.path)
        return {"task_id": task_id, "status": TaskStatus.UPLOADED, "size": stored.size, "sha256": stored.sha256}
    except UploadTooLarge as exc:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="control plane unavailable") from exc
    finally:
        async with upload_lock:
            active_uploads -= 1
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
    return {"task_id": task_id, "received": result_path.name, "final": final}


class ComputeProgress(BaseModel):
    status: TaskStatus
    progress: float = Field(ge=25, le=94)
    stage: str = Field(min_length=1, max_length=100)
    compute_node_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@app.post("/internal/progress/{task_id}", dependencies=[Depends(require_internal_key)])
async def relay_compute_progress(task_id: str, body: ComputeProgress) -> dict[str, object]:
    if body.status not in {
        TaskStatus.PROCESSING,
        TaskStatus.PACKAGING,
        TaskStatus.RETRYING,
        TaskStatus.FAILED,
    }:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported compute status")
    await report_status(
        app.state.client,
        settings,
        task_id,
        body.status,
        body.progress,
        body.stage,
        compute_node_id=body.compute_node_id,
        error_code=body.error_code,
        error_message=body.error_message,
    )
    return {"task_id": task_id, "accepted": True}


@app.get("/api/v1/results/{task_id}/{filename}")
def download_result(task_id: str, filename: str):
    # Add user ownership checks or a signed download token before exposing this route publicly.
    path = (settings.storage_root / "results" / task_id / Path(filename).name).resolve()
    result_root = (settings.storage_root / "results").resolve()
    if result_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="result not found")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")
