import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

source = Path(__file__).with_name('run_hkuds_upskill_tb21.py')
if not source.exists():
    source = Path(__file__).resolve().parents[1] / 'scripts/run_hkuds_upskill_tb21.py'
spec = importlib.util.spec_from_file_location('runner', source)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def save(path, value):
    path.write_text(json.dumps(value))


class TeacherResumeTest(unittest.TestCase):
    def test_failed_slot_is_preserved_and_only_remaining_slots_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []

            def stream(transport, body):
                calls.append(body)
                if len(calls) == 1:
                    raise RuntimeError('connection interrupted')
                message = {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': str(i), 'type': 'function', 'function': {'name': 'Write',
                     'arguments': json.dumps({'file_path': path, 'content': 'x' * 150})}}
                    for i, path in enumerate(runner.FILES)]}
                return message, {'prompt_tokens': 1, 'completion_tokens': 1}, 'tool_calls'

            runner.complete_stream = stream
            teacher = runner.ACPTeacher(None, 'deepseek-v4-flash-0731', save)
            with self.assertRaises(RuntimeError):
                teacher.run(root, 'prompt')
            path = root / 'http-00.json'
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                teacher.run(root, 'prompt')
            self.assertEqual(len(calls), 1)
            evidence = json.loads(original)
            save(root / 'recovery-http-00.json', {'request': evidence['request'], 'spent_tool_turn': 0})

            def complete(transport, body):
                if body['messages'][-1]['role'] == 'tool':
                    calls.append(body)
                    return {'role': 'assistant', 'content': 'done', 'tool_calls': []}, {}, 'stop'
                return stream(transport, body)

            runner.complete_stream = complete
            self.assertEqual(len(teacher.run(root, 'prompt')), 3)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue((root / 'http-01.json').exists())
            self.assertEqual(len(calls), 3)
            teacher.run(root, 'prompt')
            self.assertEqual(len(calls), 3)

    def test_eight_failed_slots_never_reset_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner.complete_stream = lambda *args: (_ for _ in ()).throw(RuntimeError())
            teacher = runner.ACPTeacher(None, 'deepseek-v4-flash-0731', save)
            with self.assertRaises(RuntimeError):
                teacher.run(root, 'prompt')
            body = json.loads((root / 'http-00.json').read_text())['request']
            for turn in range(8):
                save(root / f'http-{turn:02d}.json', {'status': 'pending', 'request': body})
                save(root / f'recovery-http-{turn:02d}.json', {'request': body, 'spent_tool_turn': turn})
            with self.assertRaisesRegex(ValueError, 'complete three-file ACP'):
                teacher.run(root, 'prompt')
            self.assertFalse((root / 'http-08.json').exists())


if __name__ == '__main__':
    unittest.main()
