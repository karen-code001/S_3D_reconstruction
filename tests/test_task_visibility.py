import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane import main as control
from control_plane.config import Settings, get_settings
from control_plane.database import Base, get_db
from control_plane.models import ReconstructionTask, TransferNode

ADMIN = {"X-Admin-Key": "admin-secret"}
# Beijing-time calendar days, as the admin page sends them.
DAY_25 = {"start": "2026-10-25T00:00:00+08:00", "end": "2026-10-26T00:00:00+08:00"}


def utc(text):
    # Stored naive, like SQLite returns them.
    return datetime.fromisoformat(text).astimezone(timezone.utc).replace(tzinfo=None)


class TaskVisibilityTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with self.sessions() as db:
            db.add(TransferNode(id="transfer-1", public_url="http://transfer.test"))
            for task_id, created in (
                ("before", "2026-10-24T23:59:59+08:00"),
                ("first", "2026-10-25T00:00:00+08:00"),
                ("night", "2026-10-25T23:30:00+08:00"),   # 15:30 UTC, still the 25th in Beijing
                ("after", "2026-10-26T00:00:00+08:00"),
            ):
                db.add(ReconstructionTask(id=task_id, original_filename="v.mp4", transfer_node_id="transfer-1",
                                          created_at=utc(created), updated_at=utc(created)))
            db.add(ReconstructionTask(id="legacy-null", original_filename="v.mp4", transfer_node_id="transfer-1",
                                      created_at=utc("2026-10-20T12:00:00+08:00"), hidden=None))
            db.commit()

        def db():
            with self.sessions() as session:
                yield session

        control.app.dependency_overrides[get_db] = db
        control.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, admin_key="admin-secret")
        self.addCleanup(control.app.dependency_overrides.clear)
        self.client = TestClient(control.app)

    def listed(self):
        return {t["task_id"] for t in self.client.get("/api/v1/tasks").json()}

    def test_hiding_a_day_removes_exactly_its_tasks_from_the_list(self):
        self.assertEqual(self.listed(), {"before", "first", "night", "after", "legacy-null"})
        preview = self.client.post("/api/v1/admin/tasks/visibility/preview", headers=ADMIN, json=DAY_25).json()
        self.assertEqual(preview, {"total": 2, "hidden": 0, "updated": 0})

        result = self.client.post("/api/v1/admin/tasks/visibility", headers=ADMIN, json={**DAY_25, "hidden": True}).json()
        self.assertEqual(result, {"total": 2, "hidden": 2, "updated": 2})
        self.assertEqual(self.listed(), {"before", "after", "legacy-null"})
        again = self.client.post("/api/v1/admin/tasks/visibility", headers=ADMIN, json={**DAY_25, "hidden": True}).json()
        self.assertEqual(again["updated"], 0)

        # A hidden task can still be looked up by id, flagged so pages can drop it.
        self.assertTrue(self.client.get("/api/v1/tasks/night").json()["hidden"])
        self.assertFalse(self.client.get("/api/v1/tasks/after").json()["hidden"])

        restored = self.client.post("/api/v1/admin/tasks/visibility", headers=ADMIN, json={**DAY_25, "hidden": False}).json()
        self.assertEqual(restored, {"total": 2, "hidden": 0, "updated": 2})
        self.assertEqual(len(self.listed()), 5)

    def test_daily_counts_use_the_callers_timezone(self):
        self.client.post("/api/v1/admin/tasks/visibility", headers=ADMIN, json={**DAY_25, "hidden": True})
        beijing = {d["date"]: (d["total"], d["hidden"]) for d in
                   self.client.get("/api/v1/admin/tasks/daily?tz_offset_minutes=480", headers=ADMIN).json()}
        self.assertEqual(beijing, {"2026-10-26": (1, 0), "2026-10-25": (2, 2), "2026-10-24": (1, 0), "2026-10-20": (1, 0)})
        days = self.client.get("/api/v1/admin/tasks/daily?tz_offset_minutes=0", headers=ADMIN).json()
        self.assertEqual([d["date"] for d in days], ["2026-10-25", "2026-10-24", "2026-10-20"])

    def test_admin_key_and_input_validation(self):
        for method, path in (("post", "/api/v1/admin/tasks/visibility/preview"), ("post", "/api/v1/admin/tasks/visibility"),
                             ("get", "/api/v1/admin/tasks/daily")):
            kwargs = {"json": {**DAY_25, "hidden": True}} if method == "post" else {}
            self.assertEqual(getattr(self.client, method)(path, headers={"X-Admin-Key": "wrong"}, **kwargs).status_code, 401)
        bad = (
            {"start": DAY_25["end"], "end": DAY_25["start"], "hidden": True},          # reversed
            {"start": "2026-10-25T00:00:00", "end": "2026-10-26T00:00:00", "hidden": True},  # no timezone
            {**DAY_25},                                                                # hidden missing
        )
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/v1/admin/tasks/visibility", headers=ADMIN, json=body).status_code, 422)
        self.assertEqual(len(self.listed()), 5)


if __name__ == "__main__":
    unittest.main()
