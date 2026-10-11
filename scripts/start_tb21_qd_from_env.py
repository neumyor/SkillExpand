"""Start the independent Qwen+DeepSeek QD run with process-only credentials."""
import os
import pathlib
import subprocess
import sys

root = pathlib.Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
prior_pid = os.environ.get('TB21_METHOD_ENV_PID', '1942198')
prior = dict(item.decode().split('=', 1) for item in pathlib.Path(f'/proc/{prior_pid}/environ').read_bytes().split(b'\0') if b'=' in item)
values = {}
for line in sys.stdin.read().splitlines():
    line = line.strip().removeprefix('export ')
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"\'')
method = prior.get('OPENAI_API_KEY')
qwen = values.get('OPENAI_API_KEY')
if not method or not qwen or not values.get('E2B_API_KEY'):
    raise SystemExit('missing credential roles')
env = os.environ.copy()
env.update(PYTHONPATH='src', TB21_TOTAL_WORKER_LIMIT='250',
           TBENCH_E2B_RELAY_TEMPLATE='sdt-28lb4d9k',
           TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT='1',
           TBENCH_E2B_RELAY_METADATA_JSON='{"x-mounts":"[]"}',
           TBENCH_PERSIST_SANDBOXES='0', TBENCH_TENCENT_ENV_FILE='/dev/null',
           OPENAI_API_KEY=method, TB21_EXECUTOR_API_KEY=qwen,
           E2B_API_KEY=values['E2B_API_KEY'], TB21_PIPELINE_CACHE='1',
           TB21_PIPELINE_CACHE_VERSION='pipeline-uv095-py3139-v6')
run = 'runs/tb21-qwen-deepseek-library-qwen-selector-20261011'
log = (root / run / 'driver.log').open('ab')
command = [str(root / '.venv2/bin/python'), 'scripts/run_tb21_aligned.py', '--stage', 'QD',
           '--run-dir', run, '--deepseek-source',
           '/data2/liyishan/tbench2-openclaw-min/jobs/tb21_skillexpand_20261002/tb21_e2_deepseek_raw_20261002',
           '--qwen-source', '/data2/liyishan/tb21-e1-qwen-source', '--task-file',
           'src/skillexpand/data/terminalbench/tb21.json', '--raw-gate',
           'runs/tb21-raw-gate-qwen-accepted-20261008/audit.json', '--workers', '16']
proc = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log, start_new_session=True)
print(proc.pid, flush=True)
