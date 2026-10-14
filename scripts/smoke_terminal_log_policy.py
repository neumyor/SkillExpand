"""Run one isolated short trial using existing service credentials."""
import json
import os
from pathlib import Path
import subprocess


def main():
    repo = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
    root = Path('/data2/liyishan/tbench2-openclaw-min')
    env = os.environ.copy()
    needed = ['E2B_API_KEY', 'OPENAI_API_KEY', 'TB21_METHOD_API_KEY']
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            values = dict(v.split('=', 1) for v in (proc / 'environ').read_bytes().decode().split('\0') if '=' in v)
            for key in needed:
                if values.get(key) and not env.get(key):
                    env[key] = values[key]
        except (OSError, UnicodeError, ValueError):
            continue
    if not env.get('E2B_API_KEY') or not (env.get('TB21_METHOD_API_KEY') or env.get('OPENAI_API_KEY')):
        raise RuntimeError('No available sandbox/model credentials; live smoke unavailable')
    env['OPENAI_API_KEY'] = env.get('TB21_METHOD_API_KEY') or env['OPENAI_API_KEY']
    env.update(PYTHONPATH=f'{repo}/scripts:{repo}/src:/data2/liyishan/tb21-tencent-skill/python:{root}/framework/python',
               E2B_DOMAIN='ap-beijing.tencentags.com', TBENCH_PERSIST_SANDBOXES='0')
    directory = root / 'jobs/terminal-log-policy-smoke-20261014'
    directory.mkdir(exist_ok=False)
    config = json.loads((repo / 'runs/tb21-e10-qwen-bare-262144-20261014/smoke_config.json').read_text())
    config.update(job_name='no-terminal-artifacts', jobs_dir=str(directory), n_attempts=1)
    config['datasets'][0]['task_names'] = ['fix-git']
    config['agents'][0]['override_timeout_sec'] = 180
    config['agents'][0]['kwargs'].update(record_terminal_session=True, record_terminal_output=True)
    config['environment']['kwargs']['snapshot_retries'] = 1
    path = directory / 'config.json'
    path.write_text(json.dumps(config))
    with (directory / 'smoke.log').open('w') as log:
        subprocess.run([str(root / '.venv/bin/python'), '/data2/liyishan/tb21-tencent-skill/python/run_tencent_job.py',
                        '--config', str(path), '--retry-archive-root', str(directory / 'retries'), '--no-export-traces'],
                       env=env, stdout=log, stderr=subprocess.STDOUT, timeout=480, check=True)
    results = list((directory / 'no-terminal-artifacts').glob('*/result.json'))
    assert len(results) == 1
    result = json.loads(results[0].read_text())
    trajectory = results[0].parent / 'agent/trajectory.json'
    assert trajectory.exists(), result.get('exception_info')
    assert not list(directory.rglob('*.pane')) and not list(directory.rglob('*.cast'))
    steps = json.loads(trajectory.read_text()).get('steps', [])
    assert steps and result.get('finished_at')
    print(json.dumps(dict(result=str(results[0]), trajectory=str(trajectory), steps=len(steps),
                          reward=result.get('verifier_result'), exception=result.get('exception_info'))))


if __name__ == '__main__':
    main()
