import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from sqlalchemy.exc import IntegrityError

from control_plane.main import node_heartbeat
from control_plane.models import TransferNode
from control_plane.schemas import NodeHeartbeat


class HeartbeatRegistrationTests(unittest.TestCase):
    def test_concurrent_insert_conflict_reloads_and_updates_node(self):
        existing = TransferNode(id="transfer-1", public_url="http://old.example.com")
        existing.last_heartbeat = datetime.now(timezone.utc)
        db = Mock()
        db.get.side_effect = [None, existing]
        db.commit.side_effect = [
            IntegrityError("INSERT INTO transfer_nodes", {}, Exception("duplicate id")),
            None,
        ]
        body = NodeHeartbeat(
            node_id="transfer-1",
            public_url="http://new.example.com",
            current_uploads=2,
            max_uploads=8,
            pending_tasks=3,
            network_usage_percent=12.5,
            disk_usage_percent=34.5,
        )

        result = node_heartbeat(body, db)

        self.assertEqual(result, {"status": "registered"})
        db.rollback.assert_called_once_with()
        self.assertEqual(db.commit.call_count, 2)
        self.assertEqual(existing.public_url, "http://new.example.com")
        self.assertEqual(existing.current_uploads, 2)
        self.assertEqual(existing.max_uploads, 8)
        self.assertEqual(existing.pending_tasks, 3)


if __name__ == "__main__":
    unittest.main()
