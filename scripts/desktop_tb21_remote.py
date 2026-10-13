"""Pass authorized Desktop credentials to remote process memory over SSH stdin."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess


def load(path):
    result = subprocess.run(['bash', '-c', 'set -a; source "$1"; env -0',
                             'bash', str(path)], capture_output=True, check=True)
    return dict((key, value.strip()) for key, value in
                (item.split('=', 1) for item in result.stdout.decode().split('\0') if '=' in item))


REMOTE = r'''
import json, os, runpy, sys
env = json.load(sys.stdin)
os.environ.update(env)
repo = '/data2/liyishan/SkillExpand-tb21/SkillExpand'
os.chdir(repo)
sys.path[:0] = [repo + '/src', repo + '/scripts']
if sys.argv[1] in ('recover-e7-verifiers', 'prepare-c4-cache'):
    adapter = '/data2/liyishan/tb21-tencent-skill/python'
    os.environ['PYTHONPATH'] = ':'.join([repo + '/src', repo + '/scripts', adapter,
                                      '/data2/liyishan/tbench2-openclaw-min/framework/python'])
    if sys.argv[1] == 'recover-e7-verifiers':
        command = [sys.executable, repo + '/scripts/recover_e7_verifier_slots.py',
                   '--credential-pid', str(os.getpid())]
        if env.get('TB21_RECOVERY_SLOTS'):
            command += ['--slots', *env['TB21_RECOVERY_SLOTS'].split(',')]
    else:
        command = ['/data2/liyishan/tbench2-openclaw-min/.venv/bin/python',
                   adapter + '/prepare_task_verifier_cache.py', '--credential-pid', str(os.getpid()),
                   '--version', env.get('TB21_C4_CACHE_VERSION', 'repair-20261013-v4'), '--resume-assembly', 'reshard-c4-data']
    os.execvpe(command[0], command, os.environ)
elif sys.argv[1] == 'resume-e9':
    sys.argv = [repo + '/scripts/resume_tb21_e9.py']
    runpy.run_path(sys.argv[0], run_name='__main__')
elif sys.argv[1] == 'resume-e7':
    import fcntl, subprocess, time
    from pathlib import Path
    from run_tb21_empirical import active_worker_reservation
    from summarize_tb21_empirical import read, write
    root = Path(repo) / 'runs/tb21-qwen-deepseek-library-qwen-selector-20261011/revisions/178-cards-v1'
    os.environ['OPENAI_API_KEY'] = env['TB21_METHOD_API_KEY']
    os.environ['PYTHONPATH'] = repo + '/src'
    command = [sys.executable, repo + '/scripts/run_tb21_aligned.py', '--stage', 'QD',
        '--run-dir', str(root), '--deepseek-source', read(root / 'alignment.json')['sources'][0],
        '--qwen-source', read(root / 'alignment.json')['sources'][1],
        '--task-file', read(root / 'alignment.json')['task_file'], '--workers', '16',
        '--initial-only', '--initial-slots', '77-3']
    with (Path(repo) / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                argv = proc.joinpath('cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeError):
                continue
            if '-c' not in argv and str(root) in ' '.join(argv) and any('python' in a or 'harbor' in a for a in argv[:1]):
                raise SystemExit('Existing E7 execution process blocks resume')
        reserved, _ = active_worker_reservation(Path(repo) / 'runs', root)
        if reserved + 16 > 250:
            raise SystemExit('Capacity unavailable')
        previous = len(list((root / 'slots/77-3/requests').iterdir()))
        if (root / 'targeted_resume.json').exists():
            write(root / f'targeted_resume-{time.time_ns()}.json', read(root / 'targeted_resume.json'))
        with (root / 'driver.log').open('a') as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        write(root / 'status.json', {'stage': 'QD', 'status': 'running', 'pid': child.pid,
            'workers': 16, 'phase': 'targeted_initial_resume'})
        write(root / 'targeted_resume.json', {'pid': child.pid, 'command': command,
            'slots': ['77-3'], 'previous_requests': previous, 'max_new_requests': 3,
            'budget_resets': 0, 'source': 'authorized_desktop_env', 'total_reserved': reserved + 16,
            'reason': 'prior model command transport deadline; fresh role-verified sandbox transport',
            'unresolved_setup_slots': ['51-2', '73-1', '73-2', '73-3']})
        print(json.dumps({'pid': child.pid, 'total_reserved': reserved + 16, 'slots': ['77-3']}))
else:
    from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse
    for model, role in [('qwen3.6-flash-distill', 'TB21_EXECUTOR_API_KEY'),
                        ('DEEPSEEK_up5zdj', 'TB21_METHOD_API_KEY')]:
        os.environ['OPENAI_API_KEY'] = env[role]
        relay = relay_from_env()
        try:
            relay.start()
            response = coalesce_sse(relay.transport.stream({'model': model, 'stream': True,
                'max_tokens': 64, 'messages': [{'role': 'user', 'content': 'Reply ready.'}]}))
            print(json.dumps({'model': model, 'accepted': bool(response.get('choices'))}), flush=True)
        except Exception as exc:
            print(json.dumps({'model': model, 'accepted': False, 'error_type': type(exc).__name__}), flush=True)
            raise SystemExit(1)
        finally:
            relay.close()
'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['probe', 'resume-e9', 'resume-e7',
                                         'recover-e7-verifiers', 'prepare-c4-cache'])
    args = parser.parse_args()
    desktop = Path.home() / 'Desktop'
    qwen, method = load(desktop / '.env'), load(desktop / '.env_deepseek')
    env = {'TB21_EXECUTOR_API_KEY': qwen['OPENAI_API_KEY'],
           'TB21_METHOD_API_KEY': method['OPENAI_API_KEY'],
           'E2B_API_KEY': qwen['E2B_API_KEY'], 'E2B_DOMAIN': 'ap-beijing.tencentags.com',
           'E2B_VALIDATE_API_KEY': 'false', 'TBENCH_PERSIST_SANDBOXES': '0',
           'TBENCH_TENCENT_ENV_FILE': '/dev/null',
           'TBENCH_E2B_RELAY_TEMPLATE': 'sdt-28lb4d9k',
           'TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT': '1',
           'TBENCH_E2B_RELAY_METADATA_JSON': '{"x-mounts":"[]"}',
           'EXPE_LLM_TIMEOUT_SECONDS': '180',
           'TB21_PIPELINE_CACHE': '1', 'TB21_PIPELINE_CACHE_VERSION': 'pipeline-uv095-py3139-v6'}
    if args.action == 'prepare-c4-cache':
        import os
        if os.getenv('TB21_FIXTURE_PROXY'):
            env['TB21_FIXTURE_PROXY'] = os.environ['TB21_FIXTURE_PROXY']
        if os.getenv('TB21_C4_SOURCE_FILE'):
            env['TB21_C4_SOURCE_FILE'] = os.environ['TB21_C4_SOURCE_FILE']
    if args.action == 'recover-e7-verifiers':
        import os
        if os.getenv('TB21_RECOVERY_SLOTS'):
            env['TB21_RECOVERY_SLOTS'] = os.environ['TB21_RECOVERY_SLOTS']
    command = '/data2/liyishan/SkillExpand-tb21/SkillExpand/.venv2/bin/python -c ' + shlex.quote(REMOTE) + ' ' + args.action
    result = subprocess.run(['ssh', 'jinan40', command], input=json.dumps(env),
                            text=True, capture_output=True)
    output = result.stdout + result.stderr
    for value in (env['TB21_EXECUTOR_API_KEY'], env['TB21_METHOD_API_KEY'], env['E2B_API_KEY']):
        output = output.replace(value, '[REDACTED]')
    print(output, end='')
    raise SystemExit(result.returncode)
