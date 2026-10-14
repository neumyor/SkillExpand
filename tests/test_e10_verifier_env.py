import ast
import asyncio
from pathlib import Path
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock


source = Path(__file__).resolve().parents[1] / 'scripts/e10_bare_environment.py'
tree = ast.parse(source.read_text())
helpers = ast.Module(body=[node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))], type_ignores=[])
namespace = {'re': re}
exec(compile(helpers, str(source), 'exec'), namespace)


class E10VerifierEnvTest(unittest.TestCase):
    def test_model_python_bootstrap_and_probe(self):
        environment = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(return_code=0)))
        asyncio.run(namespace['ensure_model_python'](environment))
        calls = environment.exec.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn('command -v python3', calls[0].args[0])
        self.assertIn('apt-get install -y python3-minimal', calls[0].args[0])
        self.assertIn('urllib.request, ssl, socket', calls[1].args[0])

    def test_model_python_failure_stops_before_agent(self):
        for results in [[1], [0, 1]]:
            environment = SimpleNamespace(exec=AsyncMock(side_effect=[
                SimpleNamespace(return_code=code) for code in results]))
            with self.assertRaisesRegex(RuntimeError, 'model transport Python'):
                asyncio.run(namespace['ensure_model_python'](environment))

    def test_original_verifier_is_noninteractive(self):
        command = '/tests/test.sh > /logs/verifier/test-stdout.txt 2>&1'
        original = {'EXISTING': 'preserved'}
        env = namespace['verifier_env'](command, original)
        self.assertEqual(env['EXISTING'], 'preserved')
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(env['GIT_SSH_COMMAND'], 'ssh -o BatchMode=yes -o ConnectTimeout=10')
        self.assertEqual(original, {'EXISTING': 'preserved'})
        self.assertTrue(namespace['verifier_command'](command).endswith('< /dev/null'))

    def test_agent_and_setup_commands_are_unchanged(self):
        env = {'GIT_SSH_COMMAND': 'custom'}
        for command in ['git clone user@localhost:/git/server', 'chmod +x /tests/test.sh',
                        '/tests/test.sh.other', 'echo /tests/test.sh']:
            self.assertIs(namespace['verifier_env'](command, env), env)
            self.assertEqual(namespace['verifier_command'](command), command)


if __name__ == '__main__':
    unittest.main()
