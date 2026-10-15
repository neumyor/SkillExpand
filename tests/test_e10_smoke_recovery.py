import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


source = Path(__file__).resolve().parents[1] / 'scripts/resume_tb21_e10_smoke.py'
tree = ast.parse(source.read_text())
helpers = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                          and node.name in {'smoke_plan', 'confirmed_http'}], type_ignores=[])
namespace = {'Path': Path, 'json': json,
             'launch': SimpleNamespace(REG=Path('/registration'), FULL='formal'),
             'AMENDMENT': Path('/registration/original_amendment.json')}
exec(compile(helpers, str(source), 'exec'), namespace)


class E10SmokeRecoveryTest(unittest.TestCase):
    def test_third_smoke_has_separate_artifacts(self):
        old = namespace['smoke_plan']()
        new = namespace['smoke_plan'](True)
        self.assertEqual(old[0], 'smoke_ssh_noninteractive_02')
        self.assertEqual(new[0], 'smoke_python_runtime_03')
        self.assertTrue(all(a != b for a, b in zip(old, new)))

    def test_constructed_request_is_not_http_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'agent').mkdir()
            row = {'result': str(root / 'result.json')}
            responses = root / 'agent/raw_model_responses.jsonl'
            self.assertFalse(namespace['confirmed_http'](row))
            responses.write_text(json.dumps({'http_status': 0}) + '\n')
            self.assertFalse(namespace['confirmed_http'](row))
            responses.write_text(json.dumps({'http_status': 200}) + '\n')
            self.assertTrue(namespace['confirmed_http'](row))


if __name__ == '__main__':
    unittest.main()
