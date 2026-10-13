import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'e8_audit', Path(__file__).parents[1] / 'scripts/audit_tb21_e8.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class E8AuditTests(unittest.TestCase):
    def test_user_reset_epoch_requires_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / 'authorization.json'
            authorization.write_text(json.dumps({'user_authorized': True, 'experiment': 'E8'}))
            intent = dict(original_result='old', previous_executions=0, execution_limit=2,
                          remaining_after_reservation=1, budget_epoch='user-reset-20261013',
                          authorization_file=str(authorization))
            (root / 'recovery_intent.json').write_text(json.dumps(intent))
            original = [dict(task='a', trial='a__1', reward=None, result='old')]
            recovery = [dict(task='a', trial='a__1', reward=1, result=str(root / 'result.json'))]
            self.assertEqual(audit.merge_recovery(original, recovery)[0]['reward'], 1)
            authorization.write_text(json.dumps({'user_authorized': False, 'experiment': 'E8'}))
            with self.assertRaises(ValueError):
                audit.merge_recovery(original, recovery)
    def test_recovery_is_unique_and_preserves_accepted_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = [dict(task='a', trial='a__1', reward=None, result='old'),
                        dict(task='a', trial='a__2', reward=0, result='accepted')]
            recovery = [dict(task='a', trial='a__1', reward=1, result=str(root / 'result.json'))]
            with self.assertRaises(FileNotFoundError):
                audit.merge_recovery(original, recovery)
            (root / 'recovery_intent.json').write_text(json.dumps(dict(original_result='old',
                previous_executions=1, execution_limit=2, remaining_after_reservation=0)))
            merged = audit.merge_recovery(original, recovery)
            self.assertEqual(len(merged), 2)
            self.assertEqual(merged[0]['historical_result'], 'old')
            self.assertEqual(merged[1]['result'], 'accepted')
            with self.assertRaises(ValueError):
                audit.merge_recovery(original, [dict(task='a', trial='a__2', reward=1)])
    def test_missing_metrics_stay_unknown(self):
        rows = [dict(task='a', reward=r) for r in [1, 0, None]]
        rows += [dict(task='b', reward=r) for r in [0, 0, None]]
        result = audit.metrics(rows)
        self.assertEqual(result['valid_scores'], 4)
        self.assertEqual(result['pass_at_1_bounds'], [1 / 6, 3 / 6])
        self.assertEqual(result['pass_at_3_bounds'], [0.5, 1.0])

    def test_setup_zero_rejected_and_real_timeout_reward_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root / 'fix-git__a'
            (trial / 'agent').mkdir(parents=True)
            (trial / 'verifier').mkdir()
            raw = dict(task_name='fix-git', trial_name=trial.name,
                       config=dict(agent=dict(model_name='openai/deepseek-v4-flash-0731')),
                       verifier_result=dict(rewards=dict(reward=0)),
                       exception_info=dict(exception_type='AgentTimeoutError'))
            (trial / 'result.json').write_text(json.dumps(raw))
            (trial / 'agent/trajectory.json').write_text('[{"role":"assistant"}]')
            (trial / 'verifier/reward.txt').write_text('0')
            log = trial / 'verifier/test-stdout.txt'
            log.write_text('curl: command not found')
            self.assertIsNone(audit.panel(root)[0]['reward'])
            log.write_text('test session starts\ncollected 2 items\n2 failed in 1s')
            self.assertEqual(audit.panel(root)[0]['reward'], 0)
            raw['exception_info']['exception_type'] = 'VerifierTimeoutError'
            (trial / 'result.json').write_text(json.dumps(raw))
            self.assertIsNone(audit.panel(root)[0]['reward'])


if __name__ == '__main__':
    unittest.main()
