"""One additional dispatch for each of the 13 E10 setup-missing logical slots."""
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import launch_tb21_e10 as original
from audit_tb21_e10 import read, trial, metrics

REG = original.REG
REC = REG / 'recovery_setup_timeout2_20261016'
JOB_NAME = original.FULL + '_setup_timeout2_missing13_20261016'
JOB = original.ROOT / JOB_NAME
WORKERS = 13
COUNT = 13
BASE_REPORT = REG / 'agent_timeout_scoring_amendment_20261016.json'
CORRECTION = '--metrics-correction' in sys.argv
if CORRECTION:
    REC = REG / 'recovery_setup_timeout2_metricsfix_20261016'
    JOB_NAME = original.FULL + '_setup_timeout2_metricsfix_missing12_20261016'
    JOB = original.ROOT / JOB_NAME
    WORKERS = COUNT = 12
    BASE_REPORT = REG / 'recovery_setup_timeout2_20261016/reconciled_report.json'
save = original.save


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(pid):
    proc = Path('/proc') / str(pid)
    try:
        stat = (proc / 'stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'start_time_ticks': fields[19],
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pgid': int(fields[2]), 'session_id': int(fields[3])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def matches(record):
    current = identity(record['pid'])
    return bool(current and all(record[k] == current[k] for k in
        ['pid', 'start_time_ticks', 'boot_id'] if k in record))


def selection():
    report = read(BASE_REPORT)
    rows = [r for r in report['rows'] if r['score'] is None]
    assert len(rows) == COUNT and len({r['trial'] for r in rows}) == COUNT
    assert Counter(r['exception'] for r in rows) == {
        'AgentSetupTimeoutError': 10 if CORRECTION else 11, 'TimeoutException': 2}
    assert all(r['requests'] == 0 and r['reward'] is None for r in rows)
    return sorted(rows, key=lambda r: (r['task'], r['trial']))


def build_config(rows):
    cfg = read(REG / 'full_config.json')
    cfg.update(job_name=JOB_NAME, n_attempts=1, datasets=[],
        tasks=[read(Path(r['result']))['config']['task'] for r in rows],
        agent_setup_timeout_multiplier=2.0)
    cfg['environment']['import_path'] = 'e10_setup_recovery_environment:E10SetupRecoveryEnvironment'
    cfg['orchestrator']['n_concurrent_trials'] = WORKERS
    cfg['orchestrator']['retry']['max_retries'] = 0
    assert cfg['timeout_multiplier'] == 1.0
    assert cfg['agents'][0]['override_timeout_sec'] == 7200
    return cfg


def recovery_job_class():
    from harbor.job import Job as HarborJob

    class SelectedSlotJob(HarborJob):
        def _init_metrics(self):
            from harbor.metrics.mean import Mean
            super()._init_metrics()
            for task in self._task_configs:
                source = task.source or 'adhoc'
                if not self._metrics[source]:
                    self._metrics[source].append(Mean())

        def _init_trial_configs(self):
            super()._init_trial_configs()
            selected = read(REC / 'amendment.json')['selected_rows']
            assert len(self._trial_configs) == len(selected) == COUNT
            for cfg, row in zip(self._trial_configs, selected):
                assert cfg.task.path.name == row['task']
                cfg.trial_name = row['trial']

    return SelectedSlotJob


def reconcile():
    baseline = read(BASE_REPORT)
    amendment = read(REC / 'amendment.json')
    selected = {r['trial']: r for r in amendment['selected_rows']}
    logical = deepcopy(baseline['rows'])
    attempts = []
    for i, row in enumerate(logical):
        if row['trial'] not in selected:
            continue
        path = JOB / row['trial'] / 'result.json'
        if not path.exists():
            continue
        recovered = trial(path)
        assert recovered['task'] == row['task'] and recovered['trial'] == row['trial']
        scored = recovered['reward']
        basis = 'strict_verifier_reward' if scored is not None else 'missing'
        if scored is None and recovered['exception'] == 'AgentTimeoutError':
            if set(recovered['issues']) <= {'missing_reward', 'reward_file', 'tests_not_executed'}:
                scored, basis = 0, 'user_agent_timeout_zero_20261016'
        attempts.append({**recovered, 'score': scored, 'score_basis': basis,
            'original_result': selected[row['trial']]['result']})
        if scored is not None:
            logical[i] = {**recovered, 'score': scored, 'score_basis': basis,
                'original_result': selected[row['trial']]['result']}
    metric_rows = [{**r, 'reward': r['score']} for r in logical]
    tasks = sorted({r['task'] for r in logical})
    policy = metrics(metric_rows, tasks, 3)
    policy['accounted_scores'] = policy.pop('valid')
    policy['success_rate_accounted'] = policy.pop('success_rate_valid')
    verifier_count = sum(r['score_basis'] == 'strict_verifier_reward' for r in logical)
    zero_count = sum(r['score_basis'] == 'user_agent_timeout_zero_20261016' for r in logical)
    assert len(logical) == len({r['trial'] for r in logical}) == 267
    assert len(attempts) <= COUNT
    value = {'updated_at': now(), 'stage': 'E10', 'unique_logical_slots': 267,
        'original_strict_verifier_scores': 251, 'strict_verifier_scores': verifier_count,
        'policy_derived_zero_count': zero_count, 'user_policy_metrics': policy,
        'strict_scoring_complete': verifier_count == 267,
        'policy_scoring_complete': policy['missing'] == 0,
        'recovery_result_count': len(attempts), 'recovery_planned_dispatches': COUNT,
        'recovery_rows': attempts, 'rows': logical,
        'baseline_comparable': False, 'closed_set': True,
        'environment_differences': ['setup timeout 360->720', 'Python bootstrap 300->600',
            'Python validation 30->60', f'{COUNT}-slot recovery batch'],
        'original_evidence_preserved': True, 'further_automatic_recovery_authorized': False}
    save(REC / 'reconciled_report.json', value)
    save(REC / 'checkpoint.json', {k:v for k,v in value.items() if k not in ['rows', 'recovery_rows']})
    return value


def status(state, phase, **extra):
    prior = read(REG / 'status.json')
    prior.update(stage='E10', status=state, phase=phase, pid=os.getpid(),
        workers=WORKERS if state == 'running' else 0, recovery_dir=str(REC),
        operationally_closed=state != 'running', further_retries_authorized=False,
        audit_complete=False, **extra)
    save(REG / 'status.json', prior)


async def execute():
    from functools import partial
    import e2b.sandbox_async.main as sdk
    from e2b.api.client_async import get_envd_transport
    sdk.get_transport = partial(get_envd_transport, http2=False)
    from harbor.models.job.config import JobConfig
    from tencent_task_verifier_cache import on_verification_started as task_cache
    from tencent_pipeline_cache import enabled, on_verification_started, on_trial_ended
    sys.path.insert(0, original.ADAPTER)
    from run_tencent_job import patch_result_agent_name
    cfg = JobConfig.model_validate(read(REC / 'config.json'))
    for agent in cfg.agents:
        agent.kwargs.update(record_terminal_session=False, record_terminal_output=False)
    job = recovery_job_class()(cfg)
    job.on_verification_started(task_cache)
    if enabled():
        job.on_verification_started(on_verification_started)
        job.on_trial_ended(on_trial_ended)

    async def record_start(event):
        path = REC / 'dispatches' / (event.trial_id + '.json')
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            raise RuntimeError('Duplicate dispatch blocked')
        save(path, {'trial': event.trial_id, 'started_at': now(),
            'agent_setup_timeout_multiplier': event.config.agent_setup_timeout_multiplier,
            'agent_timeout_multiplier': event.config.agent_timeout_multiplier,
            'trial_retry': 0})

    async def record_end(event):
        patch_result_agent_name(JOB / event.trial_id)
        reconcile()

    job.on_trial_started(record_start)
    job.on_trial_ended(record_end)
    await job.run()


def worker():
    try:
        status('running', 'setup_timeout2_recovery_running')
        with (REC / 'harbor.log').open('a') as log:
            child = subprocess.Popen([original.PYTHON, __file__, '--execute']
                + (['--metrics-correction'] if CORRECTION else []),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        save(REC / 'child.json', identity(child.pid))
        code = child.wait()
        result = reconcile()
        policy = result['user_policy_metrics']
        save(REC / 'completion.json', {'finished_at': now(), 'child_exit_code': code,
            'actual_dispatches': len(list((REC / 'dispatches').glob('*.json'))),
            'recovery_result_count': result['recovery_result_count'],
            'policy_scoring_complete': result['policy_scoring_complete'],
            'remaining_missing': policy['missing'], 'extra_retries': 0})
        status('scored_complete' if not code and not policy['missing'] else 'audited_incomplete',
            'setup_timeout2_recovery_finished', coverage=f"{policy['accounted_scores']}/267",
            strict_verifier_coverage=f"{result['strict_verifier_scores']}/267",
            policy_derived_zero_count=result['policy_derived_zero_count'], missing=policy['missing'],
            closure_report=str(REC / 'reconciled_report.json'))
        if code:
            raise SystemExit(code)
    except Exception as exc:
        status('needs_attention', 'setup_timeout2_recovery_failed', error_type=type(exc).__name__)
        raise


def preflight():
    from harbor.models.job.config import JobConfig
    from tencent_task_verifier_cache import load
    from tencent_pipeline_cache import load_bundle
    rows = selection()
    cfg = build_config(rows)
    JobConfig.model_validate(cfg)
    assert len(cfg['tasks']) == COUNT
    assert Counter(Path(t['path']).name for t in cfg['tasks'])['fix-git'] == (1 if CORRECTION else 2)
    original_cfg = read(REG / 'full_config.json')
    assert cfg['agents'] == original_cfg['agents'] and cfg['verifier'] == original_cfg['verifier']
    assert cfg['environment']['kwargs'] == original_cfg['environment']['kwargs']
    for task, version in original.CACHES.items():
        load(task, version)
    load_bundle('pipeline-uv095-py3139-v6')
    return rows, cfg


def launch():
    rows, cfg = preflight()
    with (original.REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if REC.exists() or JOB.exists():
            raise SystemExit('Durable recovery already exists; inspect, never relaunch')
        if CORRECTION:
            previous = REG / 'recovery_setup_timeout2_20261016'
            completion = read(previous / 'completion.json')
            assert completion['child_exit_code'] == 1 and completion['remaining_missing'] == 12
            assert 'IndexError: list index out of range' in (previous / 'harbor.log').read_text()
            for name in ['supervisor_identity.json', 'child.json']:
                assert not matches(read(previous / name))
            results = read(previous / 'reconciled_report.json')['recovery_rows']
            assert sum(r['score'] is not None for r in results) == 1
            assert all(r['exception'] == 'CancelledError' for r in results if r['score'] is None)
        for name in ['supervisor_identity.json', 'child.json']:
            record = read(REG / name)
            if record and matches(record):
                raise SystemExit('Existing strong E10 process blocks recovery')
        blocked = []
        for proc in Path('/proc').glob('[0-9]*'):
            if int(proc.name) == os.getpid():
                continue
            try:
                args = proc.joinpath('cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeError):
                continue
            if '-c' not in args and any(token in arg for arg in args for token in
                    [original.FULL, str(REG), 'run_e10_harbor.py', 'launch_tb21_e10.py',
                     'resume_tb21_e10_smoke.py', 'resume_tb21_e10_missing.py']):
                blocked.append(int(proc.name))
        if blocked:
            raise SystemExit(f'Existing E10/Harbor processes block recovery: {blocked}')
        probe = ('import json,sys;from pathlib import Path;'
            f'sys.path[:0]=[{str(original.REPO / "scripts")!r},{str(original.REPO / "src")!r}];'
            'from run_tb21_empirical import active_worker_reservation;'
            f'print(json.dumps(active_worker_reservation(Path({str(original.REPO / "runs")!r}),'
            f'Path({str(REG)!r})),default=str))')
        reserved, active = json.loads(subprocess.check_output(
            [str(original.REPO / '.venv2/bin/python'), '-c', probe], text=True))
        if reserved + WORKERS > 250:
            raise SystemExit(f'Capacity unavailable: {reserved}+{WORKERS}>250')
        REC.mkdir()
        save(REC / 'config.json', cfg)
        save(REC / 'amendment.json', {'recorded_at': now(),
            'authorization': 'User: 初始化timeout multiple *2试试，然后缺失复跑',
            'implementation_correction': ('Prior metrics source IndexError cancelled 12 trials; '
                'preserve the valid result and all cancellations; finish remaining slots once.'
                if CORRECTION else None),
            'prior_recovery_evidence': str(BASE_REPORT) if CORRECTION else None,
            'selected_rows': rows, 'additional_dispatches_per_slot': 1, 'trial_retry': 0,
            'agent_setup_timeout_multiplier': 2, 'agent_setup_seconds': 720,
            'python_bootstrap_timeout_seconds': 600, 'python_validation_timeout_seconds': 60,
            'agent_execution_seconds': 7200, 'model_and_verifier_unchanged': True,
            'budget_resets': 0, 'previous_status': read(REG / 'status.json'),
            'prior_reserved': reserved, 'reserved_total': reserved + WORKERS,
            'other_reservations': active, 'original_results_preserved': True,
            'new_job': str(JOB), 'durable_launch_refuses_automatic_restart': True})
        env = dict(os.environ)
        env.update(OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'], E2B_DOMAIN='ap-beijing.tencentags.com',
            E2B_VALIDATE_API_KEY='false', HARBOR_E2B_FS_USER='root', TBENCH_PERSIST_SANDBOXES='0',
            TB21_TOTAL_WORKER_LIMIT='250', TB21_PIPELINE_CACHE='1',
            TB21_PIPELINE_CACHE_VERSION='pipeline-uv095-py3139-v6',
            TB21_TASK_VERIFIER_CACHES=json.dumps(original.CACHES), TB21_PACKAGE_DNS='183.60.83.19',
            TBENCH_TENCENT_ENV_FILE='/dev/null', TBENCH_E2B_START_CONCURRENCY='4',
            TBENCH_E2B_RPC_CONCURRENCY='12', PYTHONPATH=':'.join([str(original.REPO / 'scripts'),
                original.ADAPTER, '/data2/liyishan/tbench2-openclaw-min/framework/python',
                str(original.REPO / 'src'), str(original.REPO / 'runs/agentbrew-tb21-evo89-test89-20261012/src')]))
        with (REC / 'supervisor.log').open('a') as log:
            child = subprocess.Popen([original.PYTHON, __file__, '--worker']
                + (['--metrics-correction'] if CORRECTION else []), env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        record = identity(child.pid)
        save(REC / 'supervisor_identity.json', record)
        save(REC / 'launch.json', {'started_at': now(), 'supervisor': record,
            'new_dispatch_budget': COUNT, 'reserved_total': reserved + WORKERS})
        # The supervisor owns subsequent status updates after acquiring the same lock.
        prior = read(REG / 'status.json')
        prior.update(status='running', phase='setup_timeout2_recovery_starting', pid=child.pid,
            workers=WORKERS, recovery_dir=str(REC), operationally_closed=False,
            further_retries_authorized=False)
        save(REG / 'status.json', prior)
        print(json.dumps({'supervisor_pid': child.pid, 'selected_missing_slots': COUNT,
            'reserved_total': reserved + WORKERS, 'new_job': str(JOB)}))


if __name__ == '__main__':
    if '--execute' in sys.argv:
        asyncio.run(execute())
    elif '--worker' in sys.argv:
        # Avoid racing the launcher's final registration write.
        with (original.REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        worker()
    elif '--audit' in sys.argv:
        r = reconcile()
        print(json.dumps({k:v for k,v in r.items() if k not in ['rows', 'recovery_rows']}))
    elif '--preflight' in sys.argv:
        r, c = preflight()
        print(json.dumps({'selected': len(r), 'model_requests': 0,
            'agent_setup_timeout_multiplier': c['agent_setup_timeout_multiplier'],
            'trial_retry': c['orchestrator']['retry']['max_retries']}))
    else:
        launch()
