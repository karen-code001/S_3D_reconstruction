import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane import main as control
from control_plane.database import Base, get_db
from control_plane.models import ReconstructionTask, TransferNode
from control_plane.schemas import NodeHeartbeat


class DynamicResultUrlTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.sessions = sessionmaker(bind=engine)
        with self.sessions() as db:
            db.add(TransferNode(id="transfer-1", public_url="http://old.test:13001"))
            db.add(ReconstructionTask(
                id="task-1", original_filename="v.mp4", transfer_node_id="transfer-1",
                status="SUCCEEDED", result_url="/api/v1/results/task-1/cloud.txt",
            ))
            db.commit()

        def get_test_db():
            with self.sessions() as db:
                yield db

        control.app.dependency_overrides[get_db] = get_test_db
        self.addCleanup(control.app.dependency_overrides.clear)
        self.client = TestClient(control.app)
        self.addCleanup(self.client.close)

    def test_task_query_uses_heartbeat_address_without_rewriting_task(self):
        link = self.client.get('/api/v1/tasks/task-1').json()['result_url']
        self.assertEqual(link, 'http://old.test:13001/api/v1/results/task-1/cloud.txt')
        self.assertEqual(self.client.get('/api/v1/tasks').json()[0]['result_url'], link)
        with self.sessions() as db:
            control.node_heartbeat(NodeHeartbeat(
                node_id='transfer-1', public_url='http://new.test:13002',
                current_uploads=0, max_uploads=4, pending_tasks=0,
                network_usage_percent=0, disk_usage_percent=0,
            ), db)
        expected = 'http://new.test:13002/api/v1/results/task-1/cloud.txt'
        self.assertEqual(self.client.get('/api/v1/tasks/task-1').json()['result_url'], expected)
        self.assertEqual(self.client.get('/api/v1/tasks').json()[0]['result_url'], expected)
        with self.sessions() as db:
            self.assertEqual(db.get(ReconstructionTask, 'task-1').result_url,
                             '/api/v1/results/task-1/cloud.txt')

    def test_absolute_result_urls_are_not_supported(self):
        with self.sessions() as db:
            db.get(ReconstructionTask, 'task-1').result_url = (
                'http://legacy.test/api/v1/results/task-1/cloud.txt'
            )
            db.commit()
        self.assertIsNone(self.client.get('/api/v1/tasks/task-1').json()['result_url'])
        self.assertEqual(self.client.get('/api/v1/results/task-1/cloud.txt').status_code, 404)

    def test_relative_result_paths_and_encoded_filenames(self):
        link = '/api/v1/results/task-1/cloud%20%231.txt'
        with self.sessions() as db:
            db.get(ReconstructionTask, 'task-1').result_url = link
            db.commit()
        self.assertEqual(self.client.get('/api/v1/tasks/task-1').json()['result_url'],
                         'http://old.test:13001' + link)

    def test_missing_node_returns_no_result_url(self):
        with self.sessions() as db:
            db.delete(db.get(TransferNode, 'transfer-1'))
            db.commit()
        self.assertIsNone(self.client.get('/api/v1/tasks/task-1').json()['result_url'])

    def test_missing_or_wrong_result_returns_404(self):
        for path in ('/api/v1/results/unknown/cloud.txt', '/api/v1/results/task-1/other.txt'):
            self.assertEqual(self.client.get(path).status_code, 404)
        with self.sessions() as db:
            db.get(ReconstructionTask, 'task-1').result_url = None
            db.commit()
        self.assertIsNone(self.client.get('/api/v1/tasks/task-1').json()['result_url'])
        self.assertEqual(self.client.get('/api/v1/results/task-1/cloud.txt').status_code, 404)


if __name__ == '__main__':
    unittest.main()
