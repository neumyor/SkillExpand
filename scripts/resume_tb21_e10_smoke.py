"""One user-authorized E10 smoke replacement, then the original formal panel."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import launch_tb21_e10 as launch

EXTRA = launch.SMOKE + '_ssh_noninteractive_02'
AMENDMENT = launch.REG / 'smoke_budget_amendment_20261014.json'


def worker():
    from audit_tb21_e10 import panel, audit
    try:
        launch.run('smoke_ssh_noninteractive_02_config.json', 'smoke_ssh_noninteractive_02')
        original = panel(launch.ROOT / launch.SMOKE)
        replacement = panel(launch.ROOT / EXTRA)
        rows = [row for row in original if row['task'] == 'fix-git'] + replacement
        complete = (len(rows) == 2 and {row['task'] for row in rows}
                    == {'fix-git', 'configure-git-webserver'}
                    and all(row['reward'] is not None for row in rows))
        launch.save(launch.REG / 'smoke_audit.json', {
            'complete': complete, 'rows': rows, 'not_formal_trials': True,
            'provider_262144_context_proven': False, 'budget_amendment': str(AMENDMENT),
            'historical_failed_rows': [row for row in original if row['task'] != 'fix-git']})
        if not complete:
            raise ValueError('Authorized replacement smoke lacks real verifier outcomes')
        if (launch.ROOT / launch.FULL).exists():
            raise ValueError('Formal job already exists; refusing duplicate start')
        launch.run('full_config.json', 'full')
        cfg = json.loads((launch.REG / 'full_config.json').read_text())
        tasks = sorted(p.name for p in Path(cfg['datasets'][0]['path']).iterdir()
                       if (p / 'task.toml').exists())
        result, report = audit(launch.ROOT / launch.FULL, launch.BASELINE, tasks,
                              launch.ROOT / 'e10_retry_archives', launch.REG)
        launch.status('accepted' if result['complete'] else 'audited_incomplete',
                      'full_finished', coverage=f"{report['E10']['valid']}/267")
    except Exception as exc:
        launch.status('needs_attention', 'failed', error_type=type(exc).__name__, reason=str(exc)[:500])
        raise


def resume():
    from tbench_eval.agentbrew.processes import process_identity, process_matches
    from harbor.models.job.config import JobConfig
    from audit_tb21_e10 import panel
    from tencent_task_verifier_cache import load
    from tencent_pipeline_cache import load_bundle

    with (launch.REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if AMENDMENT.exists() or (launch.ROOT / EXTRA).exists() or (launch.ROOT / launch.FULL).exists():
            raise SystemExit('Recovery already consumed or formal artifacts exist; inspect instead')
        previous = {}
        for name in ['supervisor_identity.json', 'child.json']:
            identity = json.loads((launch.REG / name).read_text())
            if process_matches(identity):
                raise SystemExit('Strong existing E10 process blocks recovery')
            previous[name] = identity
        matching = []
        for proc in Path('/proc').glob('[0-9]*'):
            if int(proc.name) == os.getpid():
                continue
            try:
                args = proc.joinpath('cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeError):
                continue
            if '-c' not in args and any(token in arg for arg in args for token in
                    [launch.FULL, str(launch.REG), 'run_e10_harbor.py', 'launch_tb21_e10.py',
                     'resume_tb21_e10_smoke.py']):
                matching.append(int(proc.name))
        if matching:
            raise SystemExit(f'Existing E10 supervisor/Harbor children block resume: {matching}')
        probe = ('import json,sys;from pathlib import Path;'
                 f'sys.path[:0]=[{str(launch.REPO / "scripts")!r},{str(launch.REPO / "src")!r}];'
                 'from run_tb21_empirical import active_worker_reservation;'
                 f'print(json.dumps(active_worker_reservation(Path({str(launch.REPO / "runs")!r}),'
                 f'Path({str(launch.REG)!r})),default=str))')
        reserved, active = json.loads(subprocess.check_output(
            [str(launch.REPO / '.venv2/bin/python'), '-c', probe], text=True))
        if reserved + 100 > 250:
            raise SystemExit(f'Capacity unavailable: {reserved}+100>250')
        original = panel(launch.ROOT / launch.SMOKE)
        assert len(original) == 2
        assert any(r['task'] == 'fix-git' and r['reward'] is not None for r in original)
        assert any(r['task'] == 'configure-git-webserver' and r['reward'] is None for r in original)
        cfg = json.loads((launch.REG / 'smoke_config.json').read_text())
        cfg['job_name'] = EXTRA
        cfg['datasets'][0]['task_names'] = ['configure-git-webserver']
        assert cfg['n_attempts'] == 1 and cfg['orchestrator']['retry']['max_retries'] == 0
        JobConfig.model_validate(cfg)
        for task, version in launch.CACHES.items():
            load(task, version)
        load_bundle('pipeline-uv095-py3139-v6')
        launch.save(launch.REG / 'smoke_ssh_noninteractive_02_config.json', cfg)
        launch.save(launch.REG / 'smoke_audit_before_ssh_noninteractive_02.json',
                    json.loads((launch.REG / 'smoke_audit.json').read_text()))
        launch.save(AMENDMENT, {'authorization': 'User: E10启动一下吧; one failed-unit smoke then formal',
            'task': 'configure-git-webserver', 'original_attempts': 1, 'additional_attempts': 1,
            'total_attempts': 2, 'trial_retry': 0, 'formal_budget_changed': False,
            'budget_resets': 0, 'previous_identities': previous,
            'prior_reserved': reserved, 'total_reserved': reserved + 100,
            'other_reservations': active, 'original_results_preserved': True,
            'execution_adaptation': 'verifier-only GIT_SSH_COMMAND BatchMode/ConnectTimeout and GIT_TERMINAL_PROMPT=0'})
        manifest = json.loads((launch.REG / 'manifest.json').read_text())
        manifest['smoke_budget_amendment'] = str(AMENDMENT)
        manifest['execution_adaptations'].append('verifier-only noninteractive Git SSH')
        launch.save(launch.REG / 'manifest.json', manifest)
        env = dict(os.environ)
        env.update(OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'], E2B_DOMAIN='ap-beijing.tencentags.com',
            E2B_VALIDATE_API_KEY='false', HARBOR_E2B_FS_USER='root', TBENCH_PERSIST_SANDBOXES='0',
            TB21_TOTAL_WORKER_LIMIT='250', TB21_PIPELINE_CACHE='1',
            TB21_PIPELINE_CACHE_VERSION='pipeline-uv095-py3139-v6',
            TB21_TASK_VERIFIER_CACHES=json.dumps(launch.CACHES), TB21_PACKAGE_DNS='183.60.83.19',
            TBENCH_TENCENT_ENV_FILE='/dev/null', TBENCH_E2B_START_CONCURRENCY='4',
            TBENCH_E2B_RPC_CONCURRENCY='12', PYTHONPATH=':'.join([str(launch.REPO / 'scripts'),
                launch.ADAPTER, '/data2/liyishan/tbench2-openclaw-min/framework/python',
                str(launch.REPO / 'src'), '/home/xinhaidong/agentbrew-tb21-evo89-test89-20261012/src']))
        with (launch.REG / 'supervisor.log').open('a') as log:
            child = subprocess.Popen([launch.PYTHON, __file__, '--worker'], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        launch.save(launch.REG / 'supervisor_identity.json', process_identity(child.pid))
        launch.save(launch.REG / 'status.json', {'stage': 'E10', 'status': 'running',
            'phase': 'smoke_ssh_noninteractive_02_starting', 'pid': child.pid, 'workers': 100})
        print(json.dumps({'supervisor_pid': child.pid, 'reserved_total': reserved + 100,
                          'additional_smoke_attempts': 1, 'formal_started': False}))


if __name__ == '__main__':
    worker() if '--worker' in sys.argv else resume()
