"""E10: the 1001 bare Qwen protocol with a 262144-token context."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
BASELINE = Path('/data2/liyishan/tbench2-openclaw-min/jobs/tb21_version_canary_20261001/tb21_full_after_version_canary_20261001')
ROOT = Path('/data2/liyishan/tbench2-openclaw-min/jobs/tb21_skillexpand_20261014')
REG = REPO / 'runs/tb21-e10-qwen-bare-262144-20261014'
FULL = 'tb21_e10_qwen_bare_262144_20261014'
SMOKE = FULL + '_smoke'
PYTHON = '/data2/liyishan/tbench2-openclaw-min/.venv/bin/python'
ADAPTER = '/data2/liyishan/tb21-tencent-skill/python'
CACHES = {task: 'repair-20261013-v2' for task in ['qemu-startup', 'qemu-alpine-ssh',
    'mteb-retrieve', 'torch-tensor-parallelism', 'pytorch-model-recovery']}
CACHES['reshard-c4-data'] = 'repair-20261013-v4'


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def config(source):
    cfg = json.loads(json.dumps(source))
    cfg.update(job_name=FULL, jobs_dir=str(ROOT))
    cfg['agents'][0]['kwargs']['model_info'] = {'max_input_tokens': 262144, 'max_output_tokens': 65536}
    cfg['agents'][0]['kwargs'].update(enable_summarize=True, proactive_summarization_threshold=8000)
    cfg['agents'][0]['import_path'] = 'e10_bare_environment:E10BareTerminus'
    cfg['environment']['import_path'] = 'e10_bare_environment:E10BareEnvironment'
    return cfg


def status(state, phase, **extra):
    save(REG / 'status.json', {'stage': 'E10', 'status': state, 'phase': phase,
        'pid': os.getpid(), 'workers': 100 if state == 'running' else 0, **extra})


def prepare():
    from harbor.models.job.config import JobConfig
    from tencent_task_verifier_cache import load
    from tencent_pipeline_cache import load_bundle
    source = json.loads((BASELINE / 'config.json').read_text())
    cfg = config(source)
    JobConfig.model_validate(cfg)
    for task, version in CACHES.items():
        load(task, version)
    load_bundle('pipeline-uv095-py3139-v6')
    smoke = json.loads(json.dumps(cfg))
    smoke.update(job_name=SMOKE, n_attempts=1)
    smoke['orchestrator']['n_concurrent_trials'] = 1
    smoke['orchestrator']['retry']['max_retries'] = 0
    smoke['datasets'][0]['task_names'] = ['fix-git', 'configure-git-webserver']
    save(REG / 'full_config.json', cfg)
    save(REG / 'smoke_config.json', smoke)
    save(REG / 'manifest.json', {'stage': 'E10', 'workers': 100, 'skill': None,
        'source_config': str(BASELINE / 'config.json'), 'source_context_default': 131072,
        'context_tokens': 262144, 'output_tokens': 65536, 'formal_trials': 267,
        'model': cfg['agents'][0]['model_name'], 'verifier_caches': CACHES,
        'pipeline_cache': 'pipeline-uv095-py3139-v6', 'baseline_comparable': False,
        'experiment_variable': 'max_input_tokens', 'execution_adaptations': [
            'HTTP/1.1 envd', 'Tencent DNS and mirrors', 'verifier stdin closed',
            'validated dependency and original C4 fixture caches'], 'budget_resets': 0})


def run(filename, phase):
    from tbench_eval.agentbrew.processes import process_identity
    status('running', phase)
    with (REG / (phase + '.log')).open('a') as log:
        child = subprocess.Popen([PYTHON, str(REPO / 'scripts/run_e10_harbor.py'),
            '--config', str(REG / filename), '--retry-archive-root', str(ROOT / 'e10_retry_archives'),
            '--no-export-traces', '--fail-fast'], stdout=log, stderr=subprocess.STDOUT)
        save(REG / 'child.json', {**process_identity(child.pid), 'phase': phase})
        if child.wait():
            raise RuntimeError(phase + ' failed; retained original results and retry archives')


def worker():
    from audit_tb21_e10 import panel, audit
    try:
        prepare()
        run('smoke_config.json', 'smoke')
        rows = panel(ROOT / SMOKE)
        if len(rows) != 2 or any(row['reward'] is None for row in rows):
            save(REG / 'smoke_audit.json', {'complete': False, 'rows': rows})
            raise ValueError('Smoke must have real verifier outcomes and matching agent evidence')
        save(REG / 'smoke_audit.json', {'complete': True, 'rows': rows,
            'not_formal_trials': True, 'provider_262144_context_proven': False})
        run('full_config.json', 'full')
        cfg = json.loads((REG / 'full_config.json').read_text())
        tasks = sorted(p.name for p in Path(cfg['datasets'][0]['path']).iterdir() if (p / 'task.toml').exists())
        result, report = audit(ROOT / FULL, BASELINE, tasks, ROOT / 'e10_retry_archives', REG)
        status('accepted' if result['complete'] else 'audited_incomplete', 'full_finished',
               coverage=f"{report['E10']['valid']}/267")
    except Exception as exc:
        status('needs_attention', 'failed', error_type=type(exc).__name__, reason=str(exc)[:500])
        raise


def launch():
    from run_tb21_empirical import active_worker_reservation
    from tbench_eval.agentbrew.processes import process_identity
    with (REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if REG.exists() or (ROOT / FULL).exists() or (ROOT / SMOKE).exists():
            raise SystemExit('Existing E10 artifacts block a fresh launch; inspect before recovery')
        for proc in Path('/proc').glob('[0-9]*'):
            if int(proc.name) == os.getpid():
                continue
            try:
                argv = proc.joinpath('cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeError):
                continue
            if '-c' not in argv and any(FULL in arg or str(REG) in arg for arg in argv):
                raise SystemExit('Existing E10 supervisor/driver/Harbor process blocks launch')
        reserved, active = active_worker_reservation(REPO / 'runs', REG)
        if reserved + 100 > 250:
            raise SystemExit(f'Capacity unavailable: {reserved}+100>250')
        REG.mkdir()
        ROOT.mkdir(exist_ok=True)
        save(REG / 'reservation.json', {'prior': reserved, 'new': 100,
            'total': reserved + 100, 'active': active})
        env = dict(os.environ)
        env.update(OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'], E2B_DOMAIN='ap-beijing.tencentags.com',
            E2B_VALIDATE_API_KEY='false', HARBOR_E2B_FS_USER='root', TBENCH_PERSIST_SANDBOXES='0',
            TB21_TOTAL_WORKER_LIMIT='250', TB21_PIPELINE_CACHE='1',
            TB21_PIPELINE_CACHE_VERSION='pipeline-uv095-py3139-v6',
            TB21_TASK_VERIFIER_CACHES=json.dumps(CACHES), TB21_PACKAGE_DNS='183.60.83.19',
            TBENCH_TENCENT_ENV_FILE='/dev/null', TBENCH_E2B_START_CONCURRENCY='4',
            TBENCH_E2B_RPC_CONCURRENCY='12',
            PYTHONPATH=':'.join([str(REPO / 'scripts'), ADAPTER,
                '/data2/liyishan/tbench2-openclaw-min/framework/python',
                str(REPO / 'src'), str(REPO / 'runs/agentbrew-tb21-evo89-test89-20261012/src')]))
        with (REG / 'supervisor.log').open('a') as log:
            child = subprocess.Popen([PYTHON, __file__, '--worker'], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        identity = process_identity(child.pid)
        save(REG / 'supervisor_identity.json', identity)
        save(REG / 'status.json', {'stage': 'E10', 'status': 'running', 'phase': 'preparing',
            'pid': child.pid, 'workers': 100})
        print(json.dumps({'supervisor_pid': child.pid, 'total_reserved': reserved + 100,
                          'registration': str(REG)}))


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    else:
        launch()
