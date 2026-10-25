import unittest

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane import main as control
from control_plane.config import Settings, get_settings
from control_plane.database import Base, get_db, seed_plants
from control_plane.models import ReconstructionTask, TransferNode
from transfer_node.clients import query_compute_yolo_inis
from transfer_node.config import ComputeTarget, Settings as TransferSettings

ADMIN = {"X-Admin-Key": "admin-secret"}


class PlantAdminApiTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        seed_plants(engine)
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)

        def db():
            with self.sessions() as session:
                yield session

        control.app.dependency_overrides[get_db] = db
        control.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, admin_key="admin-secret")
        self.addCleanup(control.app.dependency_overrides.clear)
        # Not entered as a context manager, so the app's lifespan (real database) does not run.
        self.client = TestClient(control.app)

    def test_public_list_includes_disabled_crops_in_order(self):
        plants = self.client.get("/api/v1/plants").json()
        self.assertEqual(plants[0]["key"], "tomato_new")
        tomato = next(p for p in plants if p["key"] == "tomato")
        self.assertEqual((tomato["enabled"], tomato["name_zh"]), (False, "番茄（旧）"))
        other = next(p for p in plants if p["key"] == "other")
        self.assertFalse(other["segment"])

    def test_admin_routes_require_the_admin_key(self):
        for method, path in (("get", "/api/v1/admin/check"), ("post", "/api/v1/admin/plants"), ("put", "/api/v1/admin/plants/eggplant"), ("delete", "/api/v1/admin/plants/eggplant")):
            for headers in ({}, {"X-Admin-Key": "wrong"}):
                with self.subTest(method=method, path=path, headers=headers):
                    self.assertEqual(getattr(self.client, method)(path, headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/admin/check", headers=ADMIN).json(), {"ok": True})

    def test_admin_key_falls_back_to_internal_key(self):
        control.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, internal_api_key="internal")
        self.assertEqual(self.client.get("/api/v1/admin/check", headers={"X-Admin-Key": "internal"}).status_code, 200)

    def test_create_update_disable_and_delete(self):
        created = self.client.post("/api/v1/admin/plants", headers=ADMIN, json={"key": "maize", "name_zh": "玉米", "yolo_ini": "maize.ini"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["sort_order"], 70)
        self.assertEqual(self.client.post("/api/v1/admin/plants", headers=ADMIN, json={"key": "maize", "name_zh": "x", "yolo_ini": "x.ini"}).status_code, 409)

        updated = self.client.put("/api/v1/admin/plants/maize", headers=ADMIN, json={"yolo_ini": "maize-v2.ini", "segment": False})
        self.assertEqual((updated.json()["yolo_ini"], updated.json()["segment"], updated.json()["name_zh"]), ("maize-v2.ini", False, "玉米"))
        disabled = self.client.put("/api/v1/admin/plants/maize", headers=ADMIN, json={"enabled": False})
        self.assertFalse(disabled.json()["enabled"])
        self.assertEqual(self.client.put("/api/v1/admin/plants/missing", headers=ADMIN, json={"enabled": False}).status_code, 404)

        self.assertEqual(self.client.delete("/api/v1/admin/plants/maize", headers=ADMIN).status_code, 204)
        self.assertNotIn("maize", [p["key"] for p in self.client.get("/api/v1/plants").json()])

    def test_crop_used_by_tasks_can_only_be_disabled(self):
        with self.sessions() as db:
            db.add(TransferNode(id="transfer-1", public_url="http://transfer.test"))
            db.add(ReconstructionTask(id="task-1", original_filename="v.mp4", plant_type="pepper", transfer_node_id="transfer-1"))
            db.commit()
        response = self.client.delete("/api/v1/admin/plants/pepper", headers=ADMIN)
        self.assertEqual(response.status_code, 409)
        self.assertIn("disable", response.json()["detail"])

    def test_invalid_values_are_rejected(self):
        for body in (
            {"key": "Bad Key", "name_zh": "x", "yolo_ini": "x.ini"},
            {"key": "maize", "name_zh": "", "yolo_ini": "x.ini"},
            {"key": "maize", "name_zh": "x", "yolo_ini": "../x.ini"},
            {"key": "maize", "name_zh": "x", "yolo_ini": "x.txt"},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/v1/admin/plants", headers=ADMIN, json=body).status_code, 422)
        self.assertEqual(self.client.put("/api/v1/admin/plants/pepper", headers=ADMIN, json={"yolo_ini": "a/b.ini"}).status_code, 422)


class ComputeIniDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_node_relays_each_compute_nodes_ini_files(self):
        async def handle(request):
            self.assertEqual(request.headers["X-Internal-Key"], "k")
            if request.url.host == "up.test":
                return httpx.Response(200, json={"files": [{"name": "a.ini", "size": 10}]})
            raise httpx.ConnectError("down")

        settings = TransferSettings(_env_file=None, internal_api_key="k")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            nodes = await query_compute_yolo_inis(client, settings, [ComputeTarget("gpu-1", "http://up.test"), ComputeTarget("gpu-2", "http://down.test")])
        self.assertEqual(nodes[0], {"node_id": "gpu-1", "reachable": True, "files": [{"name": "a.ini", "size": 10}], "error": None})
        self.assertEqual((nodes[1]["node_id"], nodes[1]["reachable"]), ("gpu-2", False))


if __name__ == "__main__":
    unittest.main()
