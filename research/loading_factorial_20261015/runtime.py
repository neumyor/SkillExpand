"""Common Tencent runtime; only library exposure and loading policy vary by arm."""
import copy
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import sys
import time

MODULE_NAME = 'loading_factorial_runtime'


def build_config(template, root, slot, task, skill_root, mode):
    if mode not in ('single', 'autonomous'):
        raise ValueError('Unknown loading mode')
    cfg = copy.deepcopy(template)
    cfg.update(job_name='loading_factorial_trial', jobs_dir=str(Path(slot) / 'jobs'), n_attempts=1)
    cfg['orchestrator']['n_concurrent_trials'] = 1
    cfg['orchestrator']['retry']['max_retries'] = 0
    agent = cfg['agents'][0]
    agent['import_path'] = MODULE_NAME + ':LoadingTerminus'
    agent['kwargs'].pop('skill_read_suffix', None)
    agent['kwargs'].update(model_info={'max_input_tokens': 262144, 'max_output_tokens': 65536},
        enable_summarize=True, proactive_summarization_threshold=8000,
        record_terminal_session=False, record_terminal_output=False, loading_mode=mode,
        skills_dir='/opt/openclaw-skills', loading_manifest=str(Path(slot) / 'load_manifest.json'))
    cfg['environment']['import_path'] = MODULE_NAME + ':LoadingEnvironment'
    cfg['environment']['kwargs'].update(skill_root=str(skill_root), loading_mode=mode)
    cfg['datasets'][0].update(path=str(Path(root) / 'frozen/tasks'), task_names=[task])
    return cfg


def _load():
    from tencent_tb2_snapshot import TencentSnapshotE2BEnvironment
    from tencent_sandbox_terminus import TencentSandboxTerminus2

    class LoadingEnvironment(TencentSnapshotE2BEnvironment):
        def __init__(self, *args, skill_root, loading_mode, **kwargs):
            self.skill_root, self.loading_mode = Path(skill_root), loading_mode
            super().__init__(*args, **kwargs)

        async def start(self, force_build):
            await super().start(force_build)
            from tencent_package_mirrors import configure
            configured = await configure(self._sandbox, 'https://mirrors.tencentyun.com')
            (self.trial_paths.trial_dir / 'package_network.json').write_text(json.dumps(configured))
            result = await self.exec('command -v python3 >/dev/null && command -v tmux >/dev/null || '
                '(DEBIAN_FRONTEND=noninteractive apt-get update && '
                'DEBIAN_FRONTEND=noninteractive apt-get install -y python3-minimal tmux)',
                user='root', timeout_sec=300)
            if result.return_code != 0:
                raise RuntimeError('Common Python/tmux bootstrap failed')
            entries = [{'path': '/opt/openclaw-skills/' + p.relative_to(self.skill_root).as_posix(),
                        'data': p.read_bytes()} for p in sorted(self.skill_root.rglob('*')) if p.is_file()]
            await self.run_with_e2b_reconnect(
                lambda: self._sandbox.files.write_files(entries, user='root'), operation_name='files.write_files')
            result = await self.exec('chmod -R a-w /opt/openclaw-skills', user='root')
            if result.return_code:
                raise RuntimeError('Cannot mount immutable library')

        async def exec(self, command, **kwargs):
            if re.match(r'^/tests/test\.sh(?:\s|$)', command):
                command += ' < /dev/null'
                kwargs['env'] = {**(kwargs.get('env') or {}), 'GIT_TERMINAL_PROMPT': '0',
                                 'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes -o ConnectTimeout=10'}
            return await super().exec(command, **kwargs)

    class LoadingTerminus(TencentSandboxTerminus2):
        def __init__(self, *args, loading_mode, loading_manifest, **kwargs):
            self.loading_mode = loading_mode
            self.manifest = json.loads(Path(loading_manifest).read_text())
            super().__init__(*args, **kwargs)

        async def _build_skills_section(self, environment):
            # Native discovery reads files internally; expose only frozen routing metadata.
            return None

        async def setup(self, environment):
            await super().setup(environment)
            (self.logs_dir / 'effective_agent_config.json').write_text(json.dumps({
                'max_input_tokens': self._llm.get_model_context_limit(),
                'max_output_tokens': self._llm.get_model_output_limit(),
                'enable_summarize': self._enable_summarize,
                'proactive_summarization_threshold': self._proactive_summarization_threshold,
                'api_base': self._tencent_api_base, 'loading_mode': self.loading_mode}))

        async def run(self, instruction, environment, context):
            root = Path(__file__).resolve().parent
            watcher = (root / 'watch_files.py').read_bytes()
            await environment._sandbox.files.write('/tmp/loading-watch.py', watcher, user='root')
            result = await environment.exec(
                'nohup python3 /tmp/loading-watch.py /opt/openclaw-skills '
                '/tmp/loading-access.jsonl /tmp/loading-ready.json > /tmp/loading-watch.log 2>&1 & '
                'for i in $(seq 1 50); do [ -f /tmp/loading-ready.json ] && exit 0; sleep .1; done; exit 1',
                user='root', timeout_sec=15)
            if result.return_code:
                raise RuntimeError('File access observer did not start')
            ready = await environment._sandbox.files.read('/tmp/loading-ready.json', user='root')
            (self.logs_dir / 'loading_observer.json').write_text(ready)
            visibility = {'at': time.time(), 'mode': self.loading_mode,
                          'catalog': self.manifest['catalog'], 'initial_suffix': self.manifest['initial_suffix'],
                          'bodies_in_initial_prompt': False, 'resource_contents_in_initial_prompt': False,
                          'observer_ready': json.loads(ready)}
            (self.logs_dir / 'loading_visibility.json').write_text(json.dumps(visibility))
            original = self._session.get_incremental_output
            async def observed_output(*args, **kwargs):
                output = await original(*args, **kwargs)
                with (self.logs_dir / 'terminal_observations.jsonl').open('a') as stream:
                    stream.write(json.dumps({'at': time.time(), 'output': output}) + '\n')
                return output
            self._session.get_incremental_output = observed_output
            try:
                await super().run(instruction.rstrip() + self.manifest['initial_suffix'], environment, context)
            finally:
                try:
                    raw = await environment._sandbox.files.read('/tmp/loading-access.jsonl', user='root')
                    (self.logs_dir / 'loading_access_events.json').write_text(
                        json.dumps([json.loads(line) for line in raw.splitlines() if line.strip()]))
                    ready = await environment._sandbox.files.read('/tmp/loading-ready.json', user='root')
                    (self.logs_dir / 'loading_observer.json').write_text(ready)
                except Exception as exc:
                    (self.logs_dir / 'loading_observer_error.json').write_text(json.dumps({'error_type': type(exc).__name__}))

    globals().update(LoadingEnvironment=LoadingEnvironment, LoadingTerminus=LoadingTerminus)


def __getattr__(name):
    if name in ('LoadingEnvironment', 'LoadingTerminus'):
        _load()
        return globals()[name]
    raise AttributeError(name)


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    sys.path[:0] = [str(root), str(root / 'frozen/runtime/tencent/python'), str(root / 'frozen/runtime/framework')]
    sys.modules[MODULE_NAME] = sys.modules[__name__]
    from functools import partial
    import e2b.sandbox_async.main as sdk
    from e2b.api.client_async import get_envd_transport
    sdk.get_transport = partial(get_envd_transport, http2=False)
    runpy.run_path(str(root / 'frozen/runtime/tencent/python/run_tencent_job.py'), run_name='__main__')
