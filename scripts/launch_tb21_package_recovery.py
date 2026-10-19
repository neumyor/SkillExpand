"""Launch only unresolved smoke slots using live Qwen executor credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    repo = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
    root = repo / 'runs/tb21-package-smoke-20261014'
    donor = int(sys.argv[1])
    original = dict(item.split('=', 1) for item in Path(f'/proc/{donor}/environ').read_bytes().decode().split('\0') if '=' in item)
    env = os.environ.copy()
    for key in ('E2B_API_KEY', 'E2B_DOMAIN', 'TB21_PIPELINE_CACHE', 'TB21_PIPELINE_CACHE_VERSION', 'TB21_TOTAL_WORKER_LIMIT'):
        if key in original:
            env[key] = original[key]
    env['OPENAI_API_KEY'] = original['TB21_EXECUTOR_API_KEY']
    env.update(TBENCH_TENCENT_ENV_FILE='/dev/null', MODEL_NAME='qwen3.6-flash-distill',
               E2B_DOMAIN='ap-beijing.tencentags.com', TBENCH_PERSIST_SANDBOXES='0',
               PYTHONPATH=f'{repo / "src"}:{repo / "scripts"}:/data2/liyishan/tb21-tencent-skill/python')
    slots = ['polyglot-rust-c/bare', 'polyglot-rust-c/historical',
             'db-wal-recovery/inline', 'db-wal-recovery/package'] + [
                 f'feal-differential-cryptanalysis/{arm}' for arm in ('bare', 'historical', 'inline', 'package')]
    command = [str(repo / '.venv2/bin/python'), str(repo / 'scripts/run_tb21_package_smoke.py'),
               '--task-file', str(root / 'tasks.json'), '--runner-script',
               '/data2/liyishan/tb21-tencent-skill/scripts/run_tencent_tb21_smoke.sh',
               '--run-dir', str(root), '--workers', '8', '--slots', *slots]
    with (root / 'recovery.log').open('a') as log:
        child = subprocess.Popen(command, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    print(json.dumps({'pid': child.pid, 'slots': slots}))


if __name__ == '__main__':
    main()
