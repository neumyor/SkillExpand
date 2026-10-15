import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hkuds_e9_recovery as recovery
import run_hkuds_upskill_tb21 as runner


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class BoundedRecoveryTest(unittest.TestCase):
    def test_only_known_source_path_difference_is_accepted(self):
        saved = {'eval_config': {'source_path': str(recovery.OLD / 'config.yaml'), 'limit': 8}}
        current = {'eval_config': {'source_path': str(recovery.BASE / 'config.yaml'), 'limit': 8}}
        self.assertIs(recovery.frozen_contract(current, saved), saved)
        current['eval_config']['limit'] = 9
        with self.assertRaises(ValueError):
            recovery.frozen_contract(current, saved)
        self.assertEqual(saved['eval_config']['source_path'], str(recovery.OLD / 'config.yaml'))

    def test_evidence_mapping_does_not_change_request(self):
        old = str(recovery.OLD / 'result.json')
        original = {'raw_result_path': old, 'request': {'raw_result_path': old},
                    'verifier_evidence': {'log_path': old}}
        mapped = recovery.evidence_paths(original)
        self.assertEqual(mapped['raw_result_path'], str(recovery.BASE / 'result.json'))
        self.assertEqual(mapped['request'], original['request'])
        self.assertEqual(original['raw_result_path'], old)

    def test_generation_marker_requires_exact_authorized_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); unit = root / 'brew/task'; path = unit / 'http-00.json'
            original = {'status': 'teacher_failed', 'error_type': 'TypeError', 'request': {'x': 1}}
            save(path, original)
            save(root / 'recovery_amendment_20261015.json', {
                'authorization': 'user_approved_plan_20261015', 'teacher_turn_limit': 8,
                'teacher_tasks': ['task'], 'teacher_failures': {'task': {path.name: original}}})
            recovery.mark_task(root, 'task', save)
            self.assertTrue(recovery.allow_failed_record(unit, path, {'x': 1}, 0))
            self.assertFalse(recovery.allow_failed_record(unit, path, {'x': 2}, 0))
            save(path, {**original, 'error_type': 'ValueError'})
            self.assertFalse(recovery.allow_failed_record(unit, path, {'x': 1}, 0))

    def test_null_function_and_incomplete_stream_diagnostics(self):
        events = [{'choices': None}, {'choices': [{'delta': {'content': 'partial',
            'tool_calls': [{'index': 0, 'function': None}]}, 'finish_reason': 'length'}]},
            {'usage': {'completion_tokens': 65536}}]
        module = SimpleNamespace(iter_sse_events=lambda chunks: iter(events))
        diagnostics = {'raw': io.BytesIO()}
        with patch.dict(sys.modules, {'skillexpand.runtime.llm_relay': module}):
            with self.assertRaisesRegex(ValueError, 'did not complete'):
                runner.complete_stream(SimpleNamespace(stream=lambda body: []), {}, diagnostics)
        self.assertEqual(diagnostics['content'], 'partial')
        self.assertEqual(diagnostics['finish_reason'], 'length')
        self.assertEqual(diagnostics['usage']['completion_tokens'], 65536)

    def test_failure_retains_raw_stream_and_redacts_error(self):
        from skillexpand.runtime.llm_relay import RelayError
        with tempfile.TemporaryDirectory() as tmp:
            def stream(body):
                yield b'data: {"choices": []}\n\n'
                raise RelayError('503 Bearer sk-testsecret')
            teacher = runner.ACPTeacher(SimpleNamespace(stream=stream), 'model', save)
            with self.assertRaises(RelayError):
                teacher.run(Path(tmp), 'prompt')
            evidence = json.loads((Path(tmp) / 'http-00.json').read_text())
            self.assertNotIn('sk-testsecret', evidence['error'])
            self.assertEqual(evidence['error_type'], 'RelayError')
            self.assertTrue((Path(tmp) / 'http-00.sse').read_bytes())
            with self.assertRaises(ValueError):
                teacher.run(Path(tmp), 'prompt')
            self.assertFalse((Path(tmp) / 'http-01.json').exists())

    def test_truncated_stream_retains_partial_response_and_usage(self):
        from skillexpand.runtime.llm_relay import RelayError
        with tempfile.TemporaryDirectory() as tmp:
            event = {'choices': [{'delta': {'content': 'partial'}, 'finish_reason': None}],
                     'usage': {'completion_tokens': 7}}
            stream = b'data: ' + json.dumps(event).encode() + b'\n\n'
            teacher = runner.ACPTeacher(SimpleNamespace(stream=lambda body: iter([stream])), 'model', save)
            with self.assertRaises(RelayError):
                teacher.run(Path(tmp), 'prompt')
            record = json.loads((Path(tmp) / 'http-00.json').read_text())
            self.assertEqual(record['diagnostics']['content'], 'partial')
            self.assertEqual(record['usage']['completion_tokens'], 7)
            self.assertFalse((Path(tmp) / 'acp.json').exists())

    def test_malformed_tool_arguments_are_recorded_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a',
                'function': {'name': 'Write', 'arguments': '{broken'}}]},
                'finish_reason': 'tool_calls'}]}
            stream = b'data: ' + json.dumps(event).encode() + b'\n\ndata: [DONE]\n\n'
            teacher = runner.ACPTeacher(SimpleNamespace(stream=lambda body: iter([stream])), 'model', save)
            with self.assertRaises(ValueError):
                teacher.run(Path(tmp), 'prompt')
            record = json.loads((Path(tmp) / 'http-00.json').read_text())
            self.assertEqual(record['status'], 'teacher_failed')
            self.assertEqual(record['diagnostics']['finish_reason'], 'tool_calls')
            self.assertFalse((Path(tmp) / 'acp.json').exists())

    def test_recovered_baseline_keeps_original_and_single_dispatch_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); task = recovery.MISSING_BASELINE[0]
            original = root / 'baseline' / task / 'rollout.json'
            save(original, {'reward': 0, 'terminal_failure': True})
            save(root / 'recovery_amendment_20261015.json', {
                'authorization': 'user_approved_plan_20261015',
                'baseline_extra_dispatches': {task: 1}})
            calls = []
            adapter = SimpleNamespace(infra_attempts=2)
            raw = root / 'raw/result.json'
            save(raw, {'verifier_result': {'rewards': {'reward': 1}}})
            (raw.parent / 'verifier').mkdir()
            (raw.parent / 'verifier/test-stdout.txt').write_text('test session starts\n1 passed')
            (raw.parent / 'verifier/reward.txt').write_text('1')
            def run(unit, request):
                calls.append((unit, adapter.infra_attempts))
                return {'reward': 1, 'raw_result_path': str(raw),
                        'verifier_evidence': {'status': 'tests_executed'}}
            adapter.run = run
            recovery.recover_baseline(root, task, adapter, {}, save)
            self.assertEqual(calls[0][1], 1)
            self.assertEqual(adapter.infra_attempts, 2)
            self.assertTrue(json.loads(original.read_text())['terminal_failure'])
            self.assertTrue((original.parent / 'recovery_selection_20261015.json').exists())


if __name__ == '__main__':
    unittest.main()
