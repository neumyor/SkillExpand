"""Launch the independent HKUDS ACP revision under the shared capacity lock."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
BASE = Path('/home/xinhaidong/agentbrew-tb21-evo89-test89-20261012')
REG = REPO / 'runs/tb21-e9-agentbrew-evo89-test89-20261012'
OUT = BASE / 'outputs/E9_hkuds_upskill_acp_evo89_test89_20261013'
SOURCE = REPO / 'sources/HKUDS-UpSkill'


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(path)


if '--worker' in sys.argv:
    sys.path.insert(0, str(BASE / 'src'))
    from tbench_eval.agentbrew.processes import process_identity
    def run(smoke):
        root = OUT / ('smoke' if smoke else 'run')
        command = [str(BASE / '.venv/bin/python'), str(REPO / 'scripts/run_hkuds_upskill_tb21.py'),
            '--source', str(SOURCE), '--root', str(root),
            '--eval-config', str(BASE / 'configs/agentbrew_tb21_evo89_test89_eval.yaml'),
            '--student-workers', '1' if smoke else '64', '--teacher-workers', '1' if smoke else '16']
        if smoke:
            command.append('--smoke')
        with (OUT / ('smoke.log' if smoke else 'driver.log')).open('a') as log:
            p = subprocess.Popen(command, cwd=BASE, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
        save(OUT / 'child.json', {**process_identity(p.pid), 'command': command,
            'phase': 'smoke' if smoke else 'formal'})
        save(REG / 'status.json', {'stage': 'E9', 'status': 'running', 'pid': os.getpid(),
             'workers': 64, 'phase': 'official_acp_smoke' if smoke else 'official_acp_formal',
             'output_root': str(OUT)})
        return p.wait()
    code = run(True)
    if code == 0:
        audit = json.loads((OUT / 'smoke/audit.json').read_text())
        if audit['complete']:
            code = run(False)
        else:
            code = 2
    save(REG / 'status.json', {'stage': 'E9', 'status': 'needs_audit' if code == 0 else 'needs_attention',
        'phase': 'official_acp_finished', 'workers': 0, 'pid': os.getpid(),
        'output_root': str(OUT), 'exit_code': code})
else:
    sys.path[:0] = [str(REPO / 'src'), str(REPO / 'scripts'), str(BASE / 'src')]
    from run_tb21_empirical import active_worker_reservation
    from tbench_eval.agentbrew.processes import process_identity, resume_readiness
    if not resume_readiness(BASE / 'outputs/E9_agentbrew_tb21_evo89_test89/run')['safe_to_open_run']:
        raise SystemExit('Old E9 rollout children remain active')
    with (REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = json.loads((REG / 'status.json').read_text())
        if Path('/proc', str(prior.get('pid'))).exists():
            raise SystemExit('E9 supervisor is still alive')
        for subdir in ('smoke', 'run'):
            if (OUT / subdir).exists() and not resume_readiness(OUT / subdir)['safe_to_open_run']:
                raise SystemExit('Official revision rollout children remain active')
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = proc.joinpath('cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeError):
                continue
            if '-c' not in argv and str(OUT) in argv:
                raise SystemExit('Official revision child is already running')
        reserved, _ = active_worker_reservation(REPO / 'runs', REG)
        if reserved + 64 > 250:
            raise SystemExit('Shared worker capacity unavailable')
        OUT.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(PYTHONPATH=':'.join([str(BASE / 'src'), str(REPO / 'src')]),
            TBENCH_LLM_KEY=env['TB21_EXECUTOR_API_KEY'], OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'],
            HARBOR_E2B_FS_USER='root', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        env.pop('TB21_AUTHORIZED_INFRA_EXTRA_ATTEMPTS', None)
        save(OUT / 'migration.json', {'reason': 'User requested HKUDS/UpSkill official source alignment',
            'prior_status': prior, 'historical_output_preserved': True,
            'official_source': 'https://github.com/HKUDS/UpSkill', 'new_formal_output': str(OUT),
            'old_results_or_memory_imported': False, 'total_reserved': reserved + 64})
        save(REG / 'official_revision.json', {'output_root': str(OUT),
             'launcher': str(REPO / 'scripts/launch_hkuds_upskill_tb21.py'),
             'source': 'https://github.com/HKUDS/UpSkill',
             'old_batch_notes_resume_disabled': True})
        with (OUT / 'supervisor.log').open('a') as log:
            p = subprocess.Popen([sys.executable, __file__, '--worker'], env=env, cwd=BASE,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        save(REG / 'status.json', {'stage': 'E9', 'status': 'running', 'workers': 64,
            'phase': 'official_acp_smoke', 'pid': p.pid, 'output_root': str(OUT)})
        print(json.dumps({'supervisor_pid': p.pid, 'total_reserved': reserved + 64,
                          'output_root': str(OUT)}))
        save(OUT / 'supervisor_identity.json', process_identity(p.pid))
