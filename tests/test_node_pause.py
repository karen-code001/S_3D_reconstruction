import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane import main as control
from control_plane.config import Settings, get_settings
from control_plane.database import Base, get_db, seed_plants
from control_plane.load_balancer import NodeSnapshot, choose_transfer_node
from control_plane.models import TransferNode, utcnow

ADMIN = {"X-Admin-Key": "admin-secret"}


def snapshot(node_id, paused=False):
    return NodeSnapshot(node_id, "http://" + node_id, True, False, 0, 4, 0, 0, 0, datetime.now(timezone.utc), paused=paused)


class PausedNodeSelectionTests(unittest.TestCase):
    def test_paused_node_is_never_chosen(self):
        self.assertEqual(choose_transfer_node([snapshot("a", paused=True), snapshot("b")], 60).id, "b")
        self.assertIsNone(choose_transfer_node([snapshot("a", paused=True)], 60))


class PauseApiTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        seed_plants(engine)
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self.register("zby", "old")

        def db():
            with self.sessions() as session:
                yield session

        control.app.dependency_overrides[get_db] = db
        control.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, admin_key="admin-secret")
        self.addCleanup(control.app.dependency_overrides.clear)
        self.client = TestClient(control.app)

    def register(self, *node_ids):
        with self.sessions() as db:
            for node_id in node_ids:
                db.merge(TransferNode(id=node_id, public_url="http://" + node_id, max_uploads=8, last_heartbeat=utcnow()))
            db.commit()

    def nodes(self):
        return {n["node_id"]: n for n in self.client.get("/api/v1/transfer-nodes").json()}

    def assigned_node(self):
        response = self.client.post("/api/v1/tasks", json={"filename": "v.mp4", "plant_type": "eggplant"})
        return response.json().get("transfer_node_id") if response.status_code == 201 else response.status_code

    def test_pause_requires_admin_key_and_a_registered_node(self):
        self.assertEqual(self.client.put("/api/v1/admin/transfer-nodes/zby/pause").status_code, 401)
        self.assertEqual(self.client.put("/api/v1/admin/transfer-nodes/zby/pause", headers={"X-Admin-Key": "wrong"}).status_code, 401)
        self.assertEqual(self.client.put("/api/v1/admin/transfer-nodes/missing/pause", headers=ADMIN).status_code, 404)

    def test_paused_node_gets_no_new_tasks_until_resumed(self):
        self.assertEqual(self.client.put("/api/v1/admin/transfer-nodes/zby/pause", headers=ADMIN).status_code, 204)
        self.assertEqual(self.client.put("/api/v1/admin/transfer-nodes/zby/pause", headers=ADMIN).status_code, 204)
        nodes = self.nodes()
        self.assertEqual((nodes["zby"]["paused"], nodes["zby"]["assignable"], nodes["zby"]["online"]), (True, False, True))
        self.assertFalse(nodes["old"]["paused"])
        for _ in range(5):
            self.assertEqual(self.assigned_node(), "old")

        self.client.put("/api/v1/admin/transfer-nodes/old/pause", headers=ADMIN)
        self.assertEqual(self.assigned_node(), 503)

        self.assertEqual(self.client.delete("/api/v1/admin/transfer-nodes/zby/pause", headers=ADMIN).status_code, 204)
        self.assertEqual(self.client.delete("/api/v1/admin/transfer-nodes/zby/pause", headers=ADMIN).status_code, 204)
        self.assertTrue(self.nodes()["zby"]["assignable"])
        self.assertEqual(self.assigned_node(), "zby")

    def test_pause_survives_control_plane_restart(self):
        self.client.put("/api/v1/admin/transfer-nodes/zby/pause", headers=ADMIN)
        # Shutdown clears registered nodes; they come back with their next heartbeat.
        with self.sessions() as db:
            db.execute(delete(TransferNode))
            db.commit()
        self.register("zby", "old")
        self.assertTrue(self.nodes()["zby"]["paused"])
        self.assertEqual(self.assigned_node(), "old")


if __name__ == "__main__":
    unittest.main()
