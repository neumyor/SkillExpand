"""E10-only runtime adaptation; no Skill files or guidance are mounted."""
import json
import re

from tencent_tb2_snapshot import TencentSnapshotE2BEnvironment
from tencent_sandbox_terminus import TencentSandboxTerminus2


def verifier_command(command):
    return command + ' < /dev/null' if re.match(r'^/tests/test\.sh(?:\s|$)', command) else command


class E10BareEnvironment(TencentSnapshotE2BEnvironment):
    async def start(self, force_build):
        await super().start(force_build)
        from tencent_package_mirrors import configure
        paths = await configure(self._sandbox, 'https://mirrors.tencentyun.com')
        path = self.trial_paths.trial_dir / 'package_network.json'
        path.write_text(json.dumps({'dns': '183.60.83.19',
            'mirror': 'https://mirrors.tencentyun.com', 'configured_paths': paths}) + '\n')

    async def exec(self, command, **kwargs):
        return await super().exec(verifier_command(command), **kwargs)


class E10BareTerminus(TencentSandboxTerminus2):
    async def setup(self, environment):
        await super().setup(environment)
        (self.logs_dir / 'effective_agent_config.json').write_text(json.dumps({
            'max_input_tokens': self._tencent_model_info['max_input_tokens'],
            'max_output_tokens': self._tencent_model_info['max_output_tokens'],
            'enable_summarize': self._enable_summarize,
            'proactive_summarization_threshold': self._proactive_summarization_threshold,
            'skill': None}) + '\n')
