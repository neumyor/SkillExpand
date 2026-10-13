import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from recover_e8_verifier_slots import recovery_config


class RecoveryConfigTests(unittest.TestCase):
    def test_saved_name_cannot_override_custom_adapter(self):
        raw = {'trial_name': 'qemu-startup__a', 'config': {'agent': {
            'name': 'terminus-2', 'import_path': 'tencent_sandbox_terminus:TencentSandboxTerminus2',
            'model_name': 'openai/deepseek-v4-flash-0731', 'kwargs': {'reasoning_effort': 'max'}}}}
        before = json.dumps(raw)
        config = recovery_config(raw, Path('/recovery'))
        self.assertIsNone(config['agent']['name'])
        self.assertEqual(config['agent']['kwargs'], raw['config']['agent']['kwargs'])
        self.assertEqual(json.dumps(raw), before)

    def test_different_adapter_blocks_before_dispatch(self):
        with self.assertRaises(ValueError):
            recovery_config({'config': {'agent': {'import_path': 'other:Agent'}}}, Path('/recovery'))
