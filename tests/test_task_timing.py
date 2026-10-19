import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from common.status import TaskStatus
from control_plane.config import Settings as ControlSettings
from control_plane.database import Base
from control_plane.main import create_task, update_task_status
from control_plane.models import TransferNode
from control_plane.schemas import TaskCreate, TaskStatusUpdate


class TaskTimingTests(unittest.TestCase):
    def test_upload_time_is_recorded_once_and_returned_with_completion(self):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        config = ControlSettings(_env_file=None)
        with Session(engine) as db:
            db.add(TransferNode(id='transfer-1', public_url='http://transfer.test', max_uploads=8))
            db.commit()
            task_id = create_task(TaskCreate(filename='video.mp4'), db, config).task_id
            views = {}
            for status in (
                TaskStatus.UPLOADING, TaskStatus.UPLOADED, TaskStatus.DISPATCHING, TaskStatus.TRANSFERRING,
                TaskStatus.PROCESSING, TaskStatus.RESULT_TRANSFERRING, TaskStatus.SUCCEEDED,
            ):
                body = TaskStatusUpdate(node_id='transfer-1', status=status, progress=50, current_stage=status.value.lower())
                with patch('builtins.print'):
                    views[status] = update_task_status(task_id, body, db)
            self.assertIsNone(views[TaskStatus.UPLOADING].uploaded_at)
            uploaded_at = views[TaskStatus.UPLOADED].uploaded_at
            self.assertIsNotNone(uploaded_at)
            done = views[TaskStatus.SUCCEEDED]
            self.assertEqual(done.uploaded_at, uploaded_at)
            self.assertGreaterEqual(done.completed_at, uploaded_at)
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
