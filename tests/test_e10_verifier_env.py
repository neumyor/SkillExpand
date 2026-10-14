import ast
from pathlib import Path
import re
import unittest


source = Path(__file__).resolve().parents[1] / 'scripts/e10_bare_environment.py'
tree = ast.parse(source.read_text())
helpers = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)], type_ignores=[])
namespace = {'re': re}
exec(compile(helpers, str(source), 'exec'), namespace)


class E10VerifierEnvTest(unittest.TestCase):
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
