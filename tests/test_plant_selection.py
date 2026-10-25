import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import BackgroundTasks, HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from common.plants import DEFAULT_PLANTS, yolo_ini_for
from common.security import create_upload_token, verify_upload_token
from control_plane.database import Base, ensure_task_columns, seed_plants
from control_plane.models import Plant, TransferNode
from control_plane.schemas import TaskCreate
from control_plane.config import Settings as ControlSettings
from control_plane.main import create_task, _task_view
from control_plane.models import ReconstructionTask
from transfer_node.clients import send_job
from transfer_node.config import ComputeTarget, Settings as TransferSettings
from transfer_node import main as transfer
from compute_node import main as compute
from compute_node.processor import run_reconstruction


# Enabled crops as seeded on first start: key -> (ini, segment).
CASES = {
    'tomato_new': ('20261020-Tomato-CEN-fewshot.ini', True),
    'eggplant': ('20261024-CENCLASS-922.ini', True),
    'pepper': ('20260720-lajiao.ini', True),
    'rapeseed': ('903_YOLO26.ini', True),
    'ginkgo': ('20260323-yinxing.ini', True),
    'other': ('903_YOLO26.ini', False),
}


def seeded_engine():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    seed_plants(engine)
    with Session(engine) as db:
        db.add(TransferNode(id='transfer-1', public_url='http://transfer.test', max_uploads=100))
        db.commit()
    return engine


class PlantTaskTests(unittest.TestCase):
    def test_database_upgrade_preserves_legacy_tasks_and_is_repeatable(self):
        engine = create_engine('sqlite://')
        with engine.begin() as c:
            c.execute(text('CREATE TABLE reconstruction_tasks (id TEXT PRIMARY KEY)'))
            c.execute(text("INSERT INTO reconstruction_tasks (id) VALUES ('old-task')"))
        ensure_task_columns(engine)
        ensure_task_columns(engine)
        with engine.connect() as c:
            self.assertEqual(
                c.execute(text('SELECT id, plant_type, task_name, group_name, uploaded_at, yolo_ini, segment FROM reconstruction_tasks')).one(),
                ('old-task', None, None, None, None, None, None),
            )
        engine.dispose()

    def test_seed_fills_empty_table_once_and_keeps_edits(self):
        engine = seeded_engine()
        with Session(engine) as db:
            self.assertEqual([p.key for p in db.query(Plant).order_by(Plant.sort_order)], [p.key for p in DEFAULT_PLANTS])
            self.assertFalse(db.get(Plant, 'tomato').enabled)
            db.get(Plant, 'eggplant').yolo_ini = 'edited.ini'
            db.delete(db.get(Plant, 'ginkgo'))
            db.commit()
        seed_plants(engine)
        with Session(engine) as db:
            self.assertEqual(db.get(Plant, 'eggplant').yolo_ini, 'edited.ini')
            self.assertIsNone(db.get(Plant, 'ginkgo'))
        engine.dispose()

    def test_create_snapshots_crop_settings_into_task_and_token(self):
        engine = seeded_engine()
        config = ControlSettings(_env_file=None)
        with Session(engine) as db:
            for plant, (ini, segment) in CASES.items():
                with self.subTest(plant=plant):
                    created = create_task(TaskCreate(filename='video.mp4', size=5, plant_type=plant), db, config)
                    self.assertEqual((created.yolo_ini, created.segment), (ini, segment))
                    claims = verify_upload_token(parse_qs(urlparse(created.upload_url).query)['token'][0], config.upload_token_secret)
                    self.assertEqual((claims.plant_type, claims.yolo_ini, claims.segment), (plant, ini, segment))
                    view = _task_view(db.get(ReconstructionTask, created.task_id))
                    self.assertEqual((view.plant_type, view.yolo_ini, view.segment), (plant, ini, segment))
        engine.dispose()

    def test_later_crop_edits_do_not_change_submitted_tasks(self):
        engine = seeded_engine()
        config = ControlSettings(_env_file=None)
        with Session(engine) as db:
            created = create_task(TaskCreate(filename='video.mp4', plant_type='eggplant'), db, config)
            db.get(Plant, 'eggplant').yolo_ini = 'newer.ini'
            db.commit()
            self.assertEqual(_task_view(db.get(ReconstructionTask, created.task_id)).yolo_ini, '20261024-CENCLASS-922.ini')
            self.assertEqual(create_task(TaskCreate(filename='video.mp4', plant_type='eggplant'), db, config).yolo_ini, 'newer.ini')
        engine.dispose()

    def test_unknown_or_disabled_crop_is_rejected(self):
        engine = seeded_engine()
        config = ControlSettings(_env_file=None)
        with Session(engine) as db:
            for plant in ('tomato', 'apple'):
                with self.subTest(plant=plant), self.assertRaises(HTTPException) as raised:
                    create_task(TaskCreate(filename='video.mp4', plant_type=plant), db, config)
                self.assertEqual(raised.exception.status_code, 422)
        engine.dispose()

    def test_legacy_tasks_resolve_their_old_ini(self):
        engine = seeded_engine()
        with Session(engine) as db:
            for plant, segment in (('rapeseed', True), ('other', False)):
                db.add(ReconstructionTask(id='legacy-' + plant, original_filename='v.mp4', plant_type=plant, transfer_node_id='transfer-1'))
                db.commit()
                view = _task_view(db.get(ReconstructionTask, 'legacy-' + plant))
                self.assertEqual((view.yolo_ini, view.segment), (yolo_ini_for(plant), segment))
        engine.dispose()

    def test_create_persists_task_name_and_group(self):
        engine = seeded_engine()
        config = ControlSettings(_env_file=None)
        with Session(engine) as db:
            created = create_task(TaskCreate(filename='video.mp4', task_name='植物多视角视频采集', group_name='05'), db, config)
            view = _task_view(db.get(ReconstructionTask, created.task_id))
            self.assertEqual((view.task_name, view.group_name), ('植物多视角视频采集', '05'))
            legacy = create_task(TaskCreate(filename='video.mp4'), db, config)
            view = _task_view(db.get(ReconstructionTask, legacy.task_id))
            self.assertEqual((view.task_name, view.group_name, view.yolo_ini, view.segment), (None, None, None, None))
        engine.dispose()

    def test_malformed_values_are_rejected_and_old_tokens_stay_unspecified(self):
        for bad in ('../../custom.ini', 'Tomato', '1abc', 'a' * 21):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                TaskCreate(filename='video.mp4', plant_type=bad)
        self.assertIsNone(TaskCreate(filename='video.mp4').plant_type)
        self.assertIsNone(yolo_ini_for(None))
        with self.assertRaises(ValueError):
            create_upload_token('t', 'node', 'secret', 60, plant_type='tomato', yolo_ini='../x.ini')
        claims = verify_upload_token(create_upload_token('old-task', 'node', 'secret', 60), 'secret')
        self.assertEqual((claims.plant_type, claims.yolo_ini, claims.segment), (None, None, None))


class PlantTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_persists_signed_settings_and_dispatch_sends_them(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = TransferSettings(_env_file=None, storage_root=Path(directory))
            sent = []

            async def handle(request):
                sent.append((await request.aread()).decode())
                return httpx.Response(202, json={'accepted': True})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with patch.object(transfer, 'settings', settings), patch.object(transfer, 'report_status', new=AsyncMock()):
                    transfer.app.state.client = client
                    transfer.app.state.dispatch_wakeup = asyncio.Event()
                    for plant, (ini, segment) in CASES.items():
                        token = create_upload_token(plant, settings.node_id, settings.upload_token_secret, 60, expected_size=5, plant_type=plant, yolo_ini=ini, segment=segment)
                        await transfer.upload_video(plant, BackgroundTasks(), token, UploadFile(filename='video.mp4', file=io.BytesIO(b'video')))
                        task_dir = Path(directory) / 'inbox' / plant
                        self.assertTrue((task_dir / '.ready').exists())
                        self.assertEqual(json.loads((task_dir / '.task.json').read_text()), {'plant_type': plant, 'yolo_ini': ini, 'segment': segment})
                        await send_job(client, settings, ComputeTarget('gpu', 'http://gpu.test'), plant, task_dir / 'source.mp4')
                        body = sent[-1]
                        self.assertIn('name="plant_type"\r\n\r\n' + plant, body)
                        self.assertIn('name="yolo_ini"\r\n\r\n' + ini, body)
                        self.assertIn('name="segment"\r\n\r\n' + ('true' if segment else 'false'), body)

    async def test_upload_verified_before_upgrade_still_dispatches_its_old_ini(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = TransferSettings(_env_file=None, storage_root=Path(directory))
            task_dir = Path(directory) / 'inbox' / 'old'
            task_dir.mkdir(parents=True)
            (task_dir / 'source.mp4').write_bytes(b'video')
            (task_dir / '.task.json').write_text(json.dumps({'plant_type': 'pepper'}))
            sent = []

            async def handle(request):
                sent.append((await request.aread()).decode())
                return httpx.Response(202, json={'accepted': True})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                await send_job(client, settings, ComputeTarget('gpu', 'http://gpu.test'), 'old', task_dir / 'source.mp4')
            self.assertIn('name="yolo_ini"\r\n\r\n20260720-lajiao.ini', sent[0])
            self.assertNotIn('name="segment"', sent[0])

    async def test_compute_passes_received_settings_to_processor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.bin'
            path.write_bytes(b'video')
            for plant, (ini, segment) in CASES.items():
                compute.active_jobs = 1
                compute.running_tasks.add('task')
                compute.app.state.client = object()
                with patch.object(compute, 'run_reconstruction', new=AsyncMock()) as runner, patch.object(compute, 'post_progress', new=AsyncMock()), patch.object(compute, 'post_result', new=AsyncMock()):
                    await compute.process_job('task', path, 'http://transfer.test/result', 'http://transfer.test/progress', plant, ini, segment)
                    runner.assert_awaited_once_with(path, path.parent / 'result-placeholder.txt', plant_type=plant, yolo_ini=ini, segment=segment)
                self.assertEqual(compute.active_jobs, 0)

    async def test_compute_rejects_malformed_selection(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=compute.app), base_url='http://compute.test') as client:
            for fields in ({'plant_type': 'Bad Key'}, {'yolo_ini': '../../custom.ini'}, {'yolo_ini': 'no-suffix'}):
                response = await client.post('/internal/jobs/task', headers={'X-Internal-Key': compute.settings.internal_api_key}, data={'callback_url': 'http://transfer.test/result', 'progress_url': 'http://transfer.test/progress', **fields}, files={'video': ('video.mp4', b'video')})
                self.assertEqual(response.status_code, 422, fields)

    async def test_placeholder_identifies_selection_without_claiming_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for plant, (ini, segment) in CASES.items():
                await run_reconstruction(path / 'input.bin', path / 'result.txt', plant_type=plant, yolo_ini=ini, segment=segment)
                result = (path / 'result.txt').read_text()
                self.assertIn('placeholder result', result)
                self.assertIn('yolo_ini=' + ini, result)
                self.assertIn(f'segment={segment}', result)


if __name__ == '__main__':
    unittest.main()
