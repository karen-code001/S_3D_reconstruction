import asyncio
from pathlib import Path

import httpx

from common.status import TaskStatus

from .config import ComputeTarget, Settings


async def report_status(
    client: httpx.AsyncClient,
    settings: Settings,
    task_id: str,
    status: TaskStatus,
    progress: float,
    stage: str,
    **extra: object,
) -> None:
    payload = {
        "node_id": settings.node_id,
        "status": status.value,
        "progress": progress,
        "current_stage": stage,
        **{key: value for key, value in extra.items() if value is not None},
    }
    response = await client.patch(
        f"{settings.control_plane_url.rstrip('/')}/internal/tasks/{task_id}/status",
        json=payload,
        headers={"X-Internal-Key": settings.internal_api_key},
    )
    response.raise_for_status()


async def choose_compute_node(client: httpx.AsyncClient, targets: list[ComputeTarget]) -> ComputeTarget | None:
    async def probe(target: ComputeTarget):
        try:
            print(f"choose_compute_node: server_id:{target.node_id}, weight:{target.weight}, url:{target.base_url}");
            response = await client.get(f"{target.base_url}/health", timeout=3)
            print(f"response: {response}");
            response.raise_for_status()
            data = response.json()
            print(f"data: {data}");
            if not data.get("accepting_tasks", True):
                return None
            active = max(int(data.get("active_jobs", 0)), 0)
            capacity = max(int(data.get("capacity", 1)), 1)
            return ((active / capacity) / max(target.weight, 0.01), target.node_id, target)
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    results = await asyncio.gather(*(probe(target) for target in targets))
    healthy = [result for result in results if result is not None]
    return min(healthy, default=None)[2] if healthy else None


async def send_job(
    client: httpx.AsyncClient,
    settings: Settings,
    target: ComputeTarget,
    task_id: str,
    source: Path,
) -> None:
    with source.open("rb") as input_file:
        response = await client.post(
            f"{target.base_url}/internal/jobs/{task_id}",
            data={
                "callback_url": f"{settings.callback_url.rstrip('/')}/internal/results/{task_id}",
                "progress_url": f"{settings.callback_url.rstrip('/')}/internal/progress/{task_id}",
            },
            files={"video": (source.name, input_file, "application/octet-stream")},
            headers={"X-Internal-Key": settings.internal_api_key},
            timeout=httpx.Timeout(connect=10, read=3600, write=3600, pool=10),
        )
    response.raise_for_status()
