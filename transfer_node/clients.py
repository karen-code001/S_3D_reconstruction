import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from common.status import TaskStatus

from .config import ComputeTarget, Settings


@dataclass(frozen=True)
class ComputeNodeAvailability:
    node_id: str
    base_url: str
    reachable: bool
    available: bool
    accepting_tasks: bool = False
    active_jobs: int | None = None
    capacity: int | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


async def probe_compute_node(
    client: httpx.AsyncClient,
    target: ComputeTarget,
) -> ComputeNodeAvailability:
    """Query one compute node and normalize its health response."""
    try:
        response = await client.get(f"{target.base_url}/health", timeout=3)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("health response must be a JSON object")

        accepting_tasks = bool(data.get("accepting_tasks", True))
        active_jobs = max(int(data.get("active_jobs", 0)), 0)
        capacity = max(int(data.get("capacity", 1)), 0)
        available = accepting_tasks and active_jobs < capacity
        reason = None
        if not accepting_tasks:
            reason = "not_accepting_tasks"
        elif active_jobs >= capacity:
            reason = "at_capacity"
        return ComputeNodeAvailability(
            node_id=target.node_id,
            base_url=target.base_url,
            reachable=True,
            available=available,
            accepting_tasks=accepting_tasks,
            active_jobs=active_jobs,
            capacity=capacity,
            reason=reason,
        )
    except httpx.HTTPStatusError:
        return ComputeNodeAvailability(
            node_id=target.node_id,
            base_url=target.base_url,
            reachable=True,
            available=False,
            reason="unhealthy_response",
        )
    except (httpx.HTTPError, ValueError, TypeError):
        return ComputeNodeAvailability(
            node_id=target.node_id,
            base_url=target.base_url,
            reachable=False,
            available=False,
            reason="unreachable_or_invalid_response",
        )


async def query_compute_node_availability(
    client: httpx.AsyncClient,
    targets: list[ComputeTarget],
) -> list[ComputeNodeAvailability]:
    return list(await asyncio.gather(*(probe_compute_node(client, target) for target in targets)))


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
    print(f'report_status for task_id {task_id}: {payload} -> {response.status_code}');
    response.raise_for_status()


async def choose_compute_node(client: httpx.AsyncClient, targets: list[ComputeTarget]) -> ComputeTarget | None:
    availability = await query_compute_node_availability(client, targets)
    candidates = []
    for target, node in zip(targets, availability):
        if not node.available or node.active_jobs is None or node.capacity is None:
            continue
        score = (node.active_jobs / node.capacity) / max(target.weight, 0.01)
        candidates.append((score, target.node_id, target))
    return min(candidates, default=None)[2] if candidates else None


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
