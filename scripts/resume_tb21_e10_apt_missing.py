"""One user-authorized recovery of E10's remaining missing slots, no budget reset."""
import asyncio
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import resume_tb21_e10_missing as base

PRIOR = base.REG / 'recovery_setup_timeout2_metricsfix_20261016'
REC = base.REG / 'recovery_apt_staged_20261016'
JOB_NAME = base.original.FULL + '_apt_staged_missing_20261016'
JOB = base.original.ROOT / JOB_NAME
EXPECTED_PRIOR_RESULTS = 12
AUTHORIZATION = 'User: 那复跑E10; remaining missing slots after staged APT diagnostics'
base.REC, base.JOB_NAME, base.JOB = REC, JOB_NAME, JOB
base.BASE_REPORT = PRIOR / 'reconciled_report.json'
base.CORRECTION = False
base.__file__ = __file__


def selection():
    report = base.read(base.BASE_REPORT)
    rows = [r for r in report['rows'] if r['score'] is None]
    if not base.read(PRIOR / 'completion.json'):
        rows = [r for r in rows if r['trial'] != 'write-compressor__rdbyqhz']
    if not rows:
        raise RuntimeError('No remaining missing slots')
    assert len(rows) == len({r['trial'] for r in rows})
    assert all(r['exception'] in ['AgentSetupTimeoutError', 'TimeoutException', 'RuntimeError', 'AgentTimeoutError'] for r in rows)
    return sorted(rows, key=lambda r: (r['task'], r['trial']))


def configure_count():
    rows = base.read(REC / 'amendment.json').get('selected_rows') if REC.exists() else selection()
    base.COUNT = base.WORKERS = len(rows)


def preflight():
    from harbor.models.job.config import JobConfig
    from tencent_task_verifier_cache import load
    from tencent_pipeline_cache import load_bundle
    rows = selection()
    cfg = base.build_config(rows)
    cfg['environment']['import_path'] = 'e10_apt_staged_environment:E10AptStagedEnvironment'
    parsed = JobConfig.model_validate(cfg)
    original = base.read(base.REG / 'full_config.json')
    assert cfg['agents'] == original['agents'] and cfg['verifier'] == original['verifier']
    assert cfg['environment']['kwargs'] == original['environment']['kwargs']
    assert cfg['orchestrator']['retry']['max_retries'] == 0
    assert parsed.agent_setup_timeout_multiplier == 2
    for task, version in base.original.CACHES.items():
        load(task, version)
    load_bundle('pipeline-uv095-py3139-v6')
    return rows, cfg


def launch():
    rows, cfg = preflight()
    with (base.original.REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if REC.exists() or JOB.exists():
            raise SystemExit('Durable APT recovery already exists; automatic relaunch prohibited')
        completion = base.read(PRIOR / 'completion.json')
        assert completion and completion['recovery_result_count'] == EXPECTED_PRIOR_RESULTS
        assert completion['remaining_missing'] == len(rows)
        for name in ['supervisor_identity.json', 'child.json']:
            assert not base.matches(base.read(PRIOR / name)), 'Prior recovery still alive'
        blocked = []
        for proc in Path('/proc').glob('[0-9]*'):
            if int(proc.name) == os.getpid():
                continue
            try:
                args = proc.joinpath('cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeError):
                continue
            if '-c' not in args and any(token in arg for arg in args for token in
                [base.original.FULL, str(base.REG), 'run_e10_harbor.py',
                 'resume_tb21_e10_missing.py', 'resume_tb21_e10_apt_missing.py',
                 'resume_tb21_e10_dbwal_once.py']):
                blocked.append(int(proc.name))
        if blocked:
            raise SystemExit(f'Existing E10 process blocks dispatch: {blocked}')
        probe = ('import json,sys;from pathlib import Path;'
            f'sys.path[:0]={[str(base.original.REPO / "scripts"), str(base.original.REPO / "src")]!r};'
            'from run_tb21_empirical import active_worker_reservation;'
            f'print(json.dumps(active_worker_reservation(Path({str(base.original.REPO / "runs")!r}),Path({str(base.REG)!r})),default=str))')
        reserved, active = json.loads(subprocess.check_output(
            [str(base.original.REPO / '.venv2/bin/python'), '-c', probe], text=True))
        assert reserved + base.WORKERS <= 250
        REC.mkdir()
        base.save(REC / 'config.json', cfg)
        base.save(REC / 'amendment.json', {'recorded_at': base.now(),
            'authorization': AUTHORIZATION,
            'selected_rows': rows, 'additional_dispatches_per_slot': 1, 'trial_retry': 0,
            'prior_recovery_evidence': str(base.BASE_REPORT), 'prior_completion': completion,
            'original_results_preserved': True, 'budget_resets': 0,
            'agent_setup_seconds': 720, 'agent_execution_seconds': 7200,
            'apt_stage_seconds': 90, 'apt_network_timeout_seconds': 15,
            'apt_lock_timeout_seconds': 10, 'transport_timeout_seconds': 115,
            'model_and_verifier_unchanged': True, 'new_job': str(JOB),
            'previous_status': base.read(base.REG / 'status.json'),
            'prior_reserved': reserved, 'reserved_total': reserved + base.WORKERS,
            'other_reservations': active, 'durable_launch_refuses_automatic_restart': True})
        env = dict(os.environ)
        env.update(OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'], E2B_DOMAIN='ap-beijing.tencentags.com',
            E2B_VALIDATE_API_KEY='false', HARBOR_E2B_FS_USER='root', TBENCH_PERSIST_SANDBOXES='0',
            TB21_TOTAL_WORKER_LIMIT='250', TB21_PIPELINE_CACHE='1',
            TB21_PIPELINE_CACHE_VERSION='pipeline-uv095-py3139-v6',
            TB21_TASK_VERIFIER_CACHES=json.dumps(base.original.CACHES), TB21_PACKAGE_DNS='183.60.83.19',
            TBENCH_TENCENT_ENV_FILE='/dev/null', TBENCH_E2B_START_CONCURRENCY='4',
            TBENCH_E2B_RPC_CONCURRENCY='12', PYTHONPATH=':'.join([str(base.original.REPO / 'scripts'),
                base.original.ADAPTER, '/data2/liyishan/tbench2-openclaw-min/framework/python',
                str(base.original.REPO / 'src'), str(base.original.REPO / 'runs/agentbrew-tb21-evo89-test89-20261012/src')]))
        with (REC / 'supervisor.log').open('a') as log:
            child = subprocess.Popen([base.original.PYTHON, __file__, '--worker'], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        record = base.identity(child.pid)
        base.save(REC / 'supervisor_identity.json', record)
        base.save(REC / 'launch.json', {'started_at': base.now(), 'supervisor': record,
            'new_dispatch_budget': len(rows), 'reserved_total': reserved + len(rows)})
        prior = base.read(base.REG / 'status.json')
        report = base.read(base.BASE_REPORT)
        prior.update(status='running', phase='apt_staged_recovery_starting', pid=child.pid,
            workers=len(rows), recovery_dir=str(REC), operationally_closed=False,
            further_retries_authorized=False, missing=len(rows),
            coverage=f"{report['user_policy_metrics']['accounted_scores']}/267",
            strict_verifier_coverage=f"{report['strict_verifier_scores']}/267",
            policy_derived_zero_count=report['policy_derived_zero_count'],
            closure_report=str(REC / 'reconciled_report.json'))
        base.save(base.REG / 'status.json', prior)
        print(json.dumps({'supervisor': record, 'selected_missing_slots': len(rows),
            'reserved_total': reserved + len(rows), 'new_job': str(JOB)}))


base.selection = selection
base.preflight = preflight
original_status = base.status
def status(state, phase, **extra):
    original_status(state, phase.replace('setup_timeout2_recovery', 'apt_staged_recovery'), **extra)
base.status = status
original_reconcile = base.reconcile
def reconcile():
    value = original_reconcile()
    value['environment_differences'] = ['setup timeout 720 retained',
        'APT update/install split; install missing Python/tmux only',
        'APT stage deadline90/network15/lock10; transport115; durable logs',
        f'{base.COUNT}-slot user-authorized recovery; retry0']
    base.save(REC / 'reconciled_report.json', value)
    base.save(REC / 'checkpoint.json', {k:v for k,v in value.items() if k not in ['rows','recovery_rows']})
    return value
base.reconcile = reconcile
configure_count()
if __name__ == '__main__':
    if '--execute' in sys.argv:
        asyncio.run(base.execute())
    elif '--worker' in sys.argv:
        with (base.original.REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        base.worker()
    elif '--audit' in sys.argv:
        result = base.reconcile()
        print(json.dumps({k:v for k,v in result.items() if k not in ['rows', 'recovery_rows']}))
    elif '--preflight' in sys.argv:
        rows, cfg = preflight()
        print(json.dumps({'missing': [r['trial'] for r in rows], 'model_requests': 0,
            'retry': cfg['orchestrator']['retry']['max_retries']}))
    else:
        launch()
