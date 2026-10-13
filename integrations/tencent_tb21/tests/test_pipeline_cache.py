import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
import tencent_pipeline_cache as cache


def bundle(folder):
    folder.mkdir()
    (folder/'part-00000').write_bytes(b'payload')
    data=dict(version=folder.name,status='ready',task=cache.TASK,uv_version='0.9.5',python_version='3.13.9',packages=cache.REQUIRED,
        chunks=[dict(name='part-00000',bytes=7)],image='same-image',architecture='x86_64')
    cache.write_json(folder/'manifest.json',data)
    return data


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_ready_identity_and_complete_chunks(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); manifest=bundle(root/'v1')
            self.assertEqual(cache.load_bundle('v1',root)[1]['status'],'ready')
            manifest['status']='candidate';cache.write_json(root/'v1/manifest.json',manifest)
            with self.assertRaises(cache.PipelineCachePreparationError):cache.load_bundle('v1',root)
            cache.load_bundle('v1',root,True)
            (root/'v1/part-00000').write_bytes(b'bad')
            with self.assertRaises(cache.PipelineCachePreparationError):cache.load_bundle('v1',root,True)
            with self.assertRaises(cache.PipelineCachePreparationError):cache.load_bundle('',root)

    def test_verifier_env_does_not_change_agent_env(self):
        environment=SimpleNamespace(_pipeline_cache_ready=True)
        original={'PATH':'/agent/bin','OTHER':'unchanged'}
        self.assertIs(cache.verifier_command_env(environment,'python agent.py',original),original)
        self.assertIs(cache.verifier_command_env(environment,'chmod +x /tests/test.sh',original),original)
        changed=cache.verifier_command_env(environment,'/tests/test.sh > /logs/verifier/test-stdout.txt 2>&1',original)
        self.assertEqual(changed['OTHER'],'unchanged')
        self.assertEqual(original['PATH'],'/agent/bin')
        self.assertEqual(changed['UV_CACHE_DIR'],cache.ROOT+'/uv-cache')

    async def test_disabled_and_other_task_do_not_prepare(self):
        with patch.dict(os.environ,{'TB21_PIPELINE_CACHE':'0'}):
            await cache.on_verification_started(SimpleNamespace(task_name=cache.TASK))
        with patch.dict(os.environ,{'TB21_PIPELINE_CACHE':'1'}):
            await cache.on_verification_started(SimpleNamespace(task_name='other-task'))

    async def test_failed_upload_three_attempts_and_no_ready(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest=bundle(root/'v1')
            writer=AsyncMock(side_effect=OSError('connection interrupted'))
            env=SimpleNamespace(task_env_config=SimpleNamespace(docker_image='same-image'),
                trial_paths=SimpleNamespace(trial_dir=root/'trial'),_sandbox=SimpleNamespace(files=SimpleNamespace(write=writer)))
            env.exec=AsyncMock(return_value=SimpleNamespace(return_code=1,stdout='',stderr='missing marker'))
            with patch.object(cache,'checked_exec',AsyncMock(return_value=SimpleNamespace(stdout='x86_64'))),patch.object(cache.asyncio,'sleep',AsyncMock()):
                with self.assertRaises(cache.PipelineCachePreparationError):await cache.prepare(env,'v1',root)
            self.assertEqual(writer.await_count,3)
            self.assertFalse(env._pipeline_cache_ready)
            self.assertEqual(json.loads((root/'trial/pipeline_cache.json').read_text())['status'],'failed')
            self.assertEqual(json.loads((root/'v1/manifest.json').read_text())['status'],'ready')

    async def test_repeat_prepare_does_not_transfer(self):
        env=SimpleNamespace(task_env_config=SimpleNamespace(docker_image='same-image'),_sandbox=SimpleNamespace(files=SimpleNamespace(write=AsyncMock())))
        env.exec=AsyncMock(return_value=SimpleNamespace(return_code=0,stdout='{"version":"v1"}'))
        with patch.object(cache,'checked_exec',AsyncMock(return_value=SimpleNamespace(stdout='x86_64'))):
            result=await cache.install_bundle(env,Path('/unused'),dict(version='v1',image='same-image',architecture='x86_64'))
        self.assertTrue(result['reused']);self.assertTrue(env._pipeline_cache_ready)
        env._sandbox.files.write.assert_not_awaited()

    async def test_timeout_is_failure_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);bundle(root/'v1')
            env=SimpleNamespace(trial_paths=SimpleNamespace(trial_dir=root/'trial'))
            async def hang(*args):await asyncio.sleep(1)
            with patch.object(cache,'PREPARE_SECONDS',.01),patch.object(cache,'install_bundle',hang):
                with self.assertRaises(cache.PipelineCachePreparationError):await cache.prepare(env,'v1',root)
            self.assertFalse(env._pipeline_cache_ready)

if __name__=='__main__':unittest.main()
