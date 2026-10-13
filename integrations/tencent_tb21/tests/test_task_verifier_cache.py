import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import tencent_task_verifier_cache as cache


class TaskCacheTests(unittest.IsolatedAsyncioTestCase):
    def test_task_mapping_and_verifier_only_environment(self):
        with patch.dict(os.environ, {'TB21_TASK_VERIFIER_CACHES': '{"torch-pipeline-parallelism":"v1"}'}):
            with self.assertRaises(ValueError):
                cache.versions()
        original = {'PATH': '/agent', 'OTHER': 'keep'}
        environment = SimpleNamespace(_task_verifier_cache_ready=True, environment_name='mteb-retrieve')
        self.assertIs(cache.command_env(environment, 'python agent.py', original), original)
        changed = cache.command_env(environment, '/tests/test.sh > /logs/verifier/test-stdout.txt', original)
        self.assertEqual(changed['UV_OFFLINE'], '1')
        self.assertEqual(changed['OTHER'], 'keep')
        self.assertEqual(original['PATH'], '/agent')
        self.assertNotIn('HF_HUB_OFFLINE', changed)
        environment.environment_name = 'reshard-c4-data'
        c4 = cache.command_env(environment, '/tests/test.sh > /logs/verifier/test-stdout.txt', original)
        self.assertEqual(c4['HF_HUB_OFFLINE'], '1')
        self.assertEqual(c4['HF_DATASETS_OFFLINE'], '1')
        self.assertEqual(c4['UV_OFFLINE'], '0')

    async def test_no_configured_cache_means_no_hook_work(self):
        with patch.dict(os.environ, {'TB21_TASK_VERIFIER_CACHES': '{}'}):
            await cache.on_verification_started(SimpleNamespace(task_name='mteb-retrieve'))

    def test_candidate_and_wrong_task_cannot_be_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'mteb-retrieve/v1'
            folder.mkdir(parents=True)
            manifest = dict(task='mteb-retrieve', version='v1', status='candidate',
                            requirements=cache.REQUIREMENTS['mteb-retrieve'], chunks=[dict(name='part-00000', bytes=1)])
            (folder / 'manifest.json').write_text(json.dumps(manifest))
            (folder / 'part-00000').write_bytes(b'a')
            with patch.object(cache, 'CACHE', root):
                with self.assertRaises(ValueError):
                    cache.load('mteb-retrieve', 'v1')
                cache.load('mteb-retrieve', 'v1', candidate=True)
                manifest['task'] = 'pytorch-model-recovery'
                (folder / 'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    cache.load('mteb-retrieve', 'v1', candidate=True)

    async def test_failed_preparation_does_not_publish_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = SimpleNamespace(environment_name='mteb-retrieve',
                trial_paths=SimpleNamespace(trial_dir=Path(directory)))
            with patch.object(cache, 'load', side_effect=ValueError('not ready')):
                with self.assertRaises(ValueError):
                    await cache.prepare(environment, 'v1')
            self.assertFalse(environment._task_verifier_cache_ready)
            self.assertEqual(json.loads((Path(directory) / 'task_verifier_cache.json').read_text())['status'], 'failed')
