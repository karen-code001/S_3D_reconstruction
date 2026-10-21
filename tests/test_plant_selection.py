import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import BackgroundTasks, UploadFile
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from common.plants import PlantType, yolo_ini_for
from common.security import create_upload_token, verify_upload_token
from control_plane.database import Base, ensure_task_columns
from control_plane.models import TransferNode
from control_plane.schemas import TaskCreate
from control_plane.config import Settings as ControlSettings
from control_plane.main import create_task, _task_view
from control_plane.models import ReconstructionTask
from transfer_node.clients import send_job
from transfer_node.config import ComputeTarget, Settings as TransferSettings
from transfer_node import main as transfer
from compute_node import main as compute
from compute_node.processor import run_reconstruction


CASES = {
    'tomato': '20260411-Tomato-006.ini',
    'eggplant': '20261020-eggplant-class.ini',
    'pepper': '20260720-lajiao.ini',
    'rapeseed': '903_YOLO26.ini',
    'ginkgo': '20260323-yinxing.ini',
    'tomato_new': '20261020-Tomato-CEN-fewshot.ini',
    'other': '903_YOLO26.ini',
}


class PlantTaskTests(unittest.TestCase):
    def test_database_upgrade_preserves_legacy_tasks_and_is_repeatable(self):
        engine = create_engine('sqlite://')
        with engine.begin() as c:
            c.execute(text('CREATE TABLE reconstruction_tasks (id TEXT PRIMARY KEY)'))
            c.execute(text("INSERT INTO reconstruction_tasks (id) VALUES ('old-task')"))
        ensure_task_columns(engine)
        ensure_task_columns(engine)
        with engine.connect() as c:
            self.assertEqual(c.execute(text('SELECT id, plant_type, task_name, group_name, uploaded_at FROM reconstruction_tasks')).one(), ('old-task', None, None, None, None))
        engine.dispose()

    def test_create_persists_selection_and_signs_upload_claim(self):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        config = ControlSettings(_env_file=None)
        with Session(engine) as db:
            db.add(TransferNode(id='transfer-1', public_url='http://transfer.test', max_uploads=8))
            db.commit()
            for plant, ini in CASES.items():
                with self.subTest(plant=plant):
                    created = create_task(TaskCreate(filename='video.mp4', size=5, plant_type=plant), db, config)
                    self.assertEqual(created.yolo_ini, ini)
                    token = parse_qs(urlparse(created.upload_url).query)['token'][0]
                    self.assertEqual(verify_upload_token(token, config.upload_token_secret).plant_type, plant)
                    row = db.get(ReconstructionTask, created.task_id)
                    self.assertEqual(row.plant_type, plant)
                    self.assertEqual(_task_view(row).yolo_ini, ini)
        engine.dispose()

    def test_create_persists_task_name_and_group(self):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        config = ControlSettings(_env_file=None)
        with Session(engine) as db:
            db.add(TransferNode(id='transfer-1', public_url='http://transfer.test', max_uploads=8))
            db.commit()
            created = create_task(TaskCreate(filename='video.mp4', task_name='植物多视角视频采集', group_name='05'), db, config)
            view = _task_view(db.get(ReconstructionTask, created.task_id))
            self.assertEqual((view.task_name, view.group_name), ('植物多视角视频采集', '05'))
            legacy = create_task(TaskCreate(filename='video.mp4'), db, config)
            view = _task_view(db.get(ReconstructionTask, legacy.task_id))
            self.assertEqual((view.task_name, view.group_name), (None, None))
        engine.dispose()

    def test_unknown_crop_is_rejected_and_legacy_client_stays_unspecified(self):
        with self.assertRaises(ValidationError):
            TaskCreate(filename='video.mp4', plant_type='../../custom.ini')
        self.assertIsNone(TaskCreate(filename='video.mp4').plant_type)
        self.assertIsNone(yolo_ini_for(None))
        token = create_upload_token('old-task', 'node', 'secret', 60)
        self.assertIsNone(verify_upload_token(token, 'secret').plant_type)


class PlantTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_persists_signed_selection_and_dispatch_sends_matching_ini(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = TransferSettings(_env_file=None, storage_root=Path(directory))
            async def handle(request):
                body = (await request.aread()).decode()
                self.assertIn('name="plant_type"\r\n\r\n' + self.plant, body)
                self.assertIn('name="yolo_ini"\r\n\r\n' + CASES[self.plant], body)
                return httpx.Response(202, json={'accepted': True})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with patch.object(transfer, 'settings', settings), patch.object(transfer, 'report_status', new=AsyncMock()):
                    transfer.app.state.client = client
                    for plant in CASES:
                        self.plant = plant
                        token = create_upload_token(plant, settings.node_id, settings.upload_token_secret, 60, expected_size=5, plant_type=plant)
                        await transfer.upload_video(plant, BackgroundTasks(), token, UploadFile(filename='video.mp4', file=io.BytesIO(b'video')))
                        task_dir = Path(directory) / 'inbox' / plant
                        self.assertTrue((task_dir / '.ready').exists())
                        self.assertEqual(json.loads((task_dir / '.task.json').read_text())['plant_type'], plant)
                        await send_job(client, settings, ComputeTarget('gpu', 'http://gpu.test'), plant, task_dir / 'source.mp4')

    async def test_compute_passes_exact_ini_to_processor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.bin'
            path.write_bytes(b'video')
            for plant, ini in CASES.items():
                compute.active_jobs = 1
                compute.running_tasks.add('task')
                compute.app.state.client = object()
                with patch.object(compute, 'run_reconstruction', new=AsyncMock()) as runner, patch.object(compute, 'post_progress', new=AsyncMock()), patch.object(compute, 'post_result', new=AsyncMock()):
                    await compute.process_job('task', path, 'http://transfer.test/result', 'http://transfer.test/progress', PlantType(plant))
                    runner.assert_awaited_once_with(path, path.parent / 'result-placeholder.txt', plant_type=plant, yolo_ini=ini)
                self.assertEqual(compute.active_jobs, 0)

    async def test_compute_rejects_unapproved_or_mismatched_filename(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=compute.app), base_url='http://compute.test') as client:
            for fields in ({'plant_type': 'tomato', 'yolo_ini': CASES['pepper']}, {'plant_type': 'unknown'}, {'yolo_ini': '../../custom.ini'}):
                response = await client.post('/internal/jobs/task', headers={'X-Internal-Key': compute.settings.internal_api_key}, data={'callback_url': 'http://transfer.test/result', 'progress_url': 'http://transfer.test/progress', **fields}, files={'video': ('video.mp4', b'video')})
                self.assertEqual(response.status_code, 422)

    async def test_placeholder_identifies_selection_without_claiming_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for plant, ini in CASES.items():
                await run_reconstruction(path / 'input.bin', path / 'result.txt', plant_type=PlantType(plant), yolo_ini=ini)
                result = (path / 'result.txt').read_text()
                self.assertIn('placeholder result', result)
                self.assertIn('yolo_ini=' + ini, result)


if __name__ == '__main__':
    unittest.main()
