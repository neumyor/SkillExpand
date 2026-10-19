"""E10 recovery-only setup deadlines; original execution and tests are retained."""
import json

from e10_bare_environment import E10BareEnvironment

BOOTSTRAP = ('command -v python3 >/dev/null 2>&1 || '
             '(DEBIAN_FRONTEND=noninteractive apt-get update && '
             'DEBIAN_FRONTEND=noninteractive apt-get install -y python3-minimal)')
VALIDATE = 'python3 -c \'import json, urllib.request, ssl, socket; print("model-python-ready")\''


def setup_timeout(command, timeout):
    if command == BOOTSTRAP and timeout == 300:
        return 600
    if command == VALIDATE and timeout == 30:
        return 60
    return timeout


class E10SetupRecoveryEnvironment(E10BareEnvironment):
    async def exec(self, command, **kwargs):
        before = kwargs.get('timeout_sec')
        after = setup_timeout(command, before)
        if after != before:
            kwargs['timeout_sec'] = after
            path = self.trial_paths.trial_dir / ('python_bootstrap_deadline.json'
                if command == BOOTSTRAP else 'python_validation_deadline.json')
            path.write_text(json.dumps({'original_timeout_sec': before,
                'effective_timeout_sec': after, 'setup_multiplier': 2}) + '\n')
        return await super().exec(command, **kwargs)
