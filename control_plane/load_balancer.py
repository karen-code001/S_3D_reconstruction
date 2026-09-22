from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


@dataclass(frozen=True)
class NodeSnapshot:
    id: str
    public_url: str
    active: bool
    draining: bool
    current_uploads: int
    max_uploads: int
    pending_tasks: int
    network_usage_percent: float
    disk_usage_percent: float
    last_heartbeat: datetime


def node_score(node: NodeSnapshot) -> float:
    upload_ratio = node.current_uploads / max(node.max_uploads, 1)
    queue_ratio = node.pending_tasks / max(node.max_uploads * 2, 1)
    return (
        0.40 * upload_ratio
        + 0.25 * (node.network_usage_percent / 100)
        + 0.20 * (node.disk_usage_percent / 100)
        + 0.15 * queue_ratio
    )


def choose_transfer_node(
    nodes: Iterable[NodeSnapshot], stale_after_seconds: int, now: datetime | None = None
) -> NodeSnapshot | None:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=stale_after_seconds)
    eligible = [
        node
        for node in nodes
        if node.active
        and not node.draining
        and node.current_uploads < node.max_uploads
        and node.disk_usage_percent < 95
        and _as_utc(node.last_heartbeat) >= cutoff
    ]
    return min(eligible, key=lambda item: (node_score(item), item.id), default=None)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

