import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import tencent_environment_setup as setup


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_setup_records_sources_and_runs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = SimpleNamespace(environment_name='task',
                trial_paths=SimpleNamespace(trial_dir=Path(directory)),
                exec=AsyncMock(return_value=SimpleNamespace(return_code=0)),
                _sandbox=SimpleNamespace(files=SimpleNamespace(read=AsyncMock(return_value='source'))))
            with patch.object(setup, 'configure', AsyncMock(return_value=['/etc/uv/uv.toml'])) as mirrors, \
                 patch.object(setup, 'configure_uv', AsyncMock(return_value=True)) as uv:
                await setup.prepare_environment(environment)
                await setup.prepare_environment(environment)
            mirrors.assert_awaited_once()
            uv.assert_awaited_once()
            self.assertTrue(environment._package_setup_ready)
            self.assertIn('"uv_cache_hit": true', (Path(directory) / 'package_setup.json').read_text())

    async def test_failure_is_recorded_and_opt_out_preserves_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = SimpleNamespace(environment_name='task',
                trial_paths=SimpleNamespace(trial_dir=Path(directory)),
                _sandbox=SimpleNamespace(files=SimpleNamespace(read=AsyncMock(return_value='original'))))
            with patch.dict(os.environ, {'TB21_PACKAGE_MIRROR_URL': ''}), \
                 patch.object(setup, 'configure', AsyncMock()) as mirrors, \
                 patch.object(setup, 'configure_uv', AsyncMock(side_effect=OSError())):
                with self.assertRaises(OSError):
                    await setup.prepare_environment(environment)
            mirrors.assert_not_awaited()
            self.assertFalse(getattr(environment, '_package_setup_ready', False))
            self.assertIn('"status": "failed"', (Path(directory) / 'package_setup.json').read_text())
