from datetime import datetime, timedelta, timezone
import unittest

from control_plane.load_balancer import NodeSnapshot, choose_transfer_node


def node(node_id: str, uploads: int, *, stale: bool = False) -> NodeSnapshot:
    return NodeSnapshot(
        id=node_id,
        public_url=f"http://{node_id}",
        active=True,
        draining=False,
        current_uploads=uploads,
        max_uploads=4,
        pending_tasks=0,
        network_usage_percent=10,
        disk_usage_percent=10,
        last_heartbeat=datetime.now(timezone.utc) - (timedelta(minutes=2) if stale else timedelta()),
    )


class LoadBalancerTests(unittest.TestCase):
    def test_selects_least_loaded_healthy_node(self):
        selected = choose_transfer_node([node("busy", 3), node("idle", 0)], 30)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "idle")

    def test_ignores_stale_nodes(self):
        selected = choose_transfer_node([node("stale", 0, stale=True), node("healthy", 1)], 30)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "healthy")


if __name__ == "__main__":
    unittest.main()
