"""E10-only runtime adaptation; no Skill files or guidance are mounted."""
import json
import re

from tencent_tb2_snapshot import TencentSnapshotE2BEnvironment
from tencent_sandbox_terminus import TencentSandboxTerminus2


def verifier_command(command):
    return command + ' < /dev/null' if re.match(r'^/tests/test\.sh(?:\s|$)', command) else command


def verifier_env(command, env=None):
    if not re.match(r'^/tests/test\.sh(?:\s|$)', command):
        return env
    return {**(env or {}), 'GIT_TERMINAL_PROMPT': '0',
            'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes -o ConnectTimeout=10'}


async def ensure_model_python(environment):
    result = await environment.exec(
        'command -v python3 >/dev/null 2>&1 || '
        '(DEBIAN_FRONTEND=noninteractive apt-get update && '
        'DEBIAN_FRONTEND=noninteractive apt-get install -y python3-minimal)',
        user='root', timeout_sec=300)
    if result.return_code != 0:
        raise RuntimeError('E10 model transport Python bootstrap failed')
    result = await environment.exec(
        "python3 -c 'import json, urllib.request, ssl, socket; print(\"model-python-ready\")'",
        user='root', timeout_sec=30)
    if result.return_code != 0:
        raise RuntimeError('E10 model transport Python validation failed')


class E10BareEnvironment(TencentSnapshotE2BEnvironment):
    async def start(self, force_build):
        await super().start(force_build)
        from tencent_package_mirrors import configure
        paths = await configure(self._sandbox, 'https://mirrors.tencentyun.com')
        path = self.trial_paths.trial_dir / 'package_network.json'
        path.write_text(json.dumps({'dns': '183.60.83.19',
            'mirror': 'https://mirrors.tencentyun.com', 'configured_paths': paths}) + '\n')

    async def exec(self, command, **kwargs):
        kwargs['env'] = verifier_env(command, kwargs.get('env'))
        return await super().exec(verifier_command(command), **kwargs)


class E10BareTerminus(TencentSandboxTerminus2):
    async def setup(self, environment):
        await ensure_model_python(environment)
        await super().setup(environment)
        (self.logs_dir / 'effective_agent_config.json').write_text(json.dumps({
            'max_input_tokens': self._tencent_model_info['max_input_tokens'],
            'max_output_tokens': self._tencent_model_info['max_output_tokens'],
            'enable_summarize': self._enable_summarize,
            'proactive_summarization_threshold': self._proactive_summarization_threshold,
            'skill': None}) + '\n')
