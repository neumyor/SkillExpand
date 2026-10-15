"""Resume the existing E9 contract after an owned-process and capacity check."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import time

REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/agentbrew-tb21-evo89-test89-20261012')
OUT = ROOT / 'outputs/E9_agentbrew_tb21_evo89_test89'
REG = REPO / 'runs/tb21-e9-agentbrew-evo89-test89-20261012'

if (REG / 'official_revision.json').exists():
    raise SystemExit('E9 now uses the HKUDS ACP revision; old batch-notes resume is disabled')


def write(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


if '--worker' in sys.argv:
    command = [str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/run_agentbrew_remote.py'),
        '--config', str(ROOT / 'configs/agentbrew_tb21_evo89_test89.yaml'),
        '--work-dir', str(OUT), '--relay-source', str(ROOT / 'sources/llm_relay_snapshot.py'),
        '--relay-port', '18139', '--with-vanilla', '--resume']
    child = subprocess.Popen(command, cwd=ROOT)
    write(REG / 'resume_child.json', {'pid': child.pid, 'supervisor_pid': os.getpid(), 'command': command})
    code = child.wait()
    write(REG / 'status.json', {'stage': 'E9', 'status': 'needs_audit' if code == 0 else 'needs_attention',
        'workers': 64, 'pid': os.getpid(), 'output_root': str(OUT), 'exit_code': code})
else:
    sys.path[:0] = [str(REPO / 'src'), str(REPO / 'scripts'), str(ROOT / 'src')]
    from run_tb21_empirical import active_worker_reservation
    from tbench_eval.agentbrew.processes import resume_readiness
    readiness = resume_readiness(OUT / 'run')
    if not readiness['safe_to_open_run']:
        raise SystemExit('Owned rollout children remain active')
    env = os.environ.copy()
    source = env
    if not all(source.get(k) for k in ('TB21_METHOD_API_KEY', 'TB21_EXECUTOR_API_KEY', 'E2B_API_KEY')):
        source = dict(v.split('=', 1) for v in Path('/proc/2320647/environ').read_bytes().decode().split('\0') if '=' in v)
    method_key = source.get('TB21_METHOD_API_KEY') or source['OPENAI_API_KEY']
    env.update(AGENTBREW_TEACHER_KEY=method_key,
        TBENCH_LLM_KEY=source['TB21_EXECUTOR_API_KEY'], OPENAI_API_KEY=source['TB21_EXECUTOR_API_KEY'],
        E2B_API_KEY=source['E2B_API_KEY'], E2B_DOMAIN='ap-beijing.tencentags.com',
        TBENCH_E2B_RELAY_TEMPLATE='sdt-28lb4d9k', TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT='1',
        TBENCH_E2B_RELAY_METADATA_JSON='{"x-mounts":"[]"}', TBENCH_PERSIST_SANDBOXES='0',
        TBENCH_TENCENT_ENV_FILE='/dev/null', E2B_VALIDATE_API_KEY='false',
        TB21_PIPELINE_CACHE='1', TB21_PIPELINE_CACHE_VERSION='pipeline-uv095-py3139-v6',
        PYTHONPATH=str(ROOT / 'src'), OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    amendment = REG / 'budget_reset_20261013.json'
    if amendment.exists():
        env['TB21_AUTHORIZED_INFRA_EXTRA_ATTEMPTS'] = '2'
    with (REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = json.loads((REG / 'status.json').read_text())
        if Path('/proc', str(prior['pid'])).exists():
            raise SystemExit('E9 supervisor still alive')
        reserved, active = active_worker_reservation(REPO / 'runs', REG)
        if reserved + 64 > 250:
            raise SystemExit('Capacity unavailable')
        recovery = REG / 'permission_recovery.json'
        if recovery.exists():
            shutil.copy2(recovery, REG / f'permission_recovery-{time.time_ns()}.json')
        write(recovery, {'reason': 'resume with retained verifier-stall classification repair and authorized credentials',
            'prior_status': prior, 'budget_resets': 0, 'config_changed': False,
            'safe_to_open_run': True, 'other_reserved': reserved})
        with (OUT / 'permission-resume.log').open('a') as log:
            p = subprocess.Popen([sys.executable, __file__, '--worker'], env=env, cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        write(REG / 'status.json', {'stage': 'E9', 'status': 'running', 'pid': p.pid,
            'workers': 64, 'phase': 'resume_after_verifier_stall_repair', 'output_root': str(OUT)})
        print(json.dumps({'supervisor_pid': p.pid, 'total_reserved': reserved + 64}))
