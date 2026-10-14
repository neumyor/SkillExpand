import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('audit', Path(__file__).resolve().parents[1] / 'scripts/audit_tb21_e10.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class E10AuditTest(unittest.TestCase):
    def test_timeout_real_reward_is_valid_but_no_tests_zero_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'task__a'
            (root / 'agent').mkdir(parents=True)
            (root / 'verifier').mkdir()
            raw = {'task_name': 'task', 'trial_name': root.name,
                   'config': {'agent': {'model_name': 'openai/qwen3.6-flash-distill'}},
                   'verifier_result': {'rewards': {'reward': 0}},
                   'exception_info': {'exception_type': 'AgentTimeoutError'}}
            (root / 'result.json').write_text(json.dumps(raw))
            (root / 'agent/trajectory.json').write_text('{"steps": [1]}')
            (root / 'verifier/reward.txt').write_text('0')
            stdout = root / 'verifier/test-stdout.txt'
            stdout.write_text('test session starts\n1 failed in 1s')
            self.assertEqual(audit.trial(root / 'result.json', False)['reward'], 0)
            stdout.write_text('dependency install failed')
            self.assertIsNone(audit.trial(root / 'result.json', False)['reward'])
            stdout.write_text('test session starts\n1 failed in 1s')
            raw['exception_info']['exception_type'] = 'VerifierTimeoutError'
            (root / 'result.json').write_text(json.dumps(raw))
            self.assertIsNone(audit.trial(root / 'result.json', False)['reward'])


if __name__ == '__main__':
    unittest.main()
