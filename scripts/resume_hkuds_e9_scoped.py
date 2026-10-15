"""Resume official E9 with the single user-authorized baseline budget exception."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
BASE = REPO / 'runs/agentbrew-tb21-evo89-test89-20261012'
OUT = BASE / 'outputs/E9_hkuds_upskill_acp_evo89_test89_20261013'
REG = REPO / 'runs/tb21-e9-agentbrew-evo89-test89-20261012'
sys.path[:0] = [str(REPO / 'src'), str(REPO / 'scripts'), str(BASE / 'src')]
from summarize_tb21_empirical import read, write
from run_tb21_empirical import active_worker_reservation
from tbench_eval.agentbrew.processes import process_identity, process_matches, resume_readiness


def registration(status, phase, pid, **details):
    write(REG / 'status.json', {'stage': 'E9', 'status': status, 'phase': phase,
          'pid': pid, 'workers': 64 if status == 'running' else 0,
          'output_root': str(OUT), **details})


if '--worker' in sys.argv:
    command = [str(BASE / '.venv/bin/python'), str(REPO / 'scripts/run_hkuds_upskill_tb21.py'),
               '--source', str(REPO / 'sources/HKUDS-UpSkill'), '--root', str(OUT / 'run'),
               '--eval-config', str(BASE / 'configs/agentbrew_tb21_evo89_test89_eval.yaml'),
               '--student-workers', '64', '--teacher-workers', '16']
    if '--brew-unstarted-only' in sys.argv:
        command.append('--brew-unstarted-only')
    with (OUT / 'driver.log').open('a') as log:
        child = subprocess.Popen(command, cwd=BASE, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    write(OUT / 'child.json', {**process_identity(child.pid), 'command': command, 'phase': 'formal'})
    registration('running', 'official_acp_formal_scoped_resume', os.getpid(), driver_pid=child.pid)
    code = child.wait()
    with (REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registration('needs_audit' if code == 0 else 'needs_attention',
                     'official_acp_finished', os.getpid(), exit_code=code)
else:
    if read(OUT / 'smoke/audit.json').get('complete') is not True:
        raise SystemExit('The existing smoke audit must pass')
    with (REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for directory in [BASE / 'outputs/E9_agentbrew_tb21_evo89_test89/run',
                          OUT / 'smoke', OUT / 'run']:
            if directory.exists() and not resume_readiness(directory)['safe_to_open_run']:
                raise SystemExit('Existing E9 rollout processes block resume')
        for identity_path in [OUT / 'child.json', OUT / 'supervisor_identity.json']:
            if identity_path.exists() and process_matches(read(identity_path)):
                raise SystemExit('Existing E9 driver or supervisor blocks resume')
        for proc in Path('/proc').glob('[0-9]*'):
            if int(proc.name) == os.getpid():
                continue
            try:
                argv = proc.joinpath('cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeError):
                continue
            if '-c' not in argv and any(str(OUT) in a for a in argv):
                raise SystemExit('Unregistered official E9 process blocks resume')
        reserved, active = active_worker_reservation(REPO / 'runs', REG)
        if reserved + 64 > 250:
            raise SystemExit('Shared worker capacity unavailable')
        if '--brew-unstarted-only' in sys.argv:
            untouched = []
            for path in (OUT / 'run/baseline').glob('*/rollout.json'):
                trial = read(path)
                unit_root = OUT / 'run/brew' / path.parent.name
                if (not trial.get('terminal_failure') and not trial.get('infra')
                        and trial.get('reward') in (0, 1)
                        and trial.get('verifier_evidence', {}).get('status') == 'tests_executed'
                        and not (unit_root / 'acp.json').exists()
                        and not any(unit_root.glob('http-*.json'))):
                    untouched.append(path.parent.name)
            if not untouched:
                raise SystemExit('No untouched eligible Brew unit remains')
        unit = OUT / 'run/baseline/configure-git-webserver'
        amendment = {'unit': str(unit), 'authorized_by': 'user_explicit_20261014',
                     'base_attempts': 2, 'extra_attempts': 1, 'total_attempts': 3,
                     'budget_resets': 0, 'phase': 'baseline', 'task': unit.name,
                     'reason': 'User approved one extra attempt after verifier stdin fix'}
        path = unit / 'budget_amendment_20261014.json'
        if path.exists() and read(path) != amendment:
            raise SystemExit('Existing budget amendment differs')
        for i in (1, 2):
            result = read(unit / f'physical_networkfix_{i:02d}/result.json')
            if result.get('infra') is not True or result.get('reward') is not None:
                raise SystemExit('Prior attempts must remain unresolved infrastructure failures')
        if (unit / 'physical_networkfix_03').exists():
            result = read(unit / 'rollout.json')
            if ('--resume-existing' not in sys.argv or result.get('infra') is not False
                    or result.get('raw_reward') not in (0, 1)
                    or result.get('verifier_evidence', {}).get('status') != 'tests_executed'):
                raise SystemExit('The single authorized extra attempt has already been dispatched')
        write(path, amendment)
        env = dict(os.environ)
        env.pop('TB21_AUTHORIZED_INFRA_EXTRA_ATTEMPTS', None)
        env.update(PYTHONPATH=':'.join([str(BASE / 'src'), str(REPO / 'src')]),
                   TBENCH_LLM_KEY=env['TB21_EXECUTOR_API_KEY'],
                   OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'], HARBOR_E2B_FS_USER='root',
                   OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
                   TBENCH_RELAY_MAX_CONCURRENCY='1')
        stamp = str(time.time_ns())
        if '--resume-existing' in sys.argv and '--brew-unstarted-only' not in sys.argv:
            for path in (OUT / 'run').glob('**/http-*.json'):
                evidence = read(path)
                if evidence.get('status') not in ('pending', 'transport_failed'):
                    continue
                if (evidence.get('status') == 'transport_failed'
                        and evidence.get('error_type') != 'RelayError'):
                    continue
                turn = int(path.stem.split('-')[-1])
                if not 0 <= turn < 8:
                    raise SystemExit('Teacher request exceeds preserved eight-turn budget')
                recovery = path.with_name('recovery-' + path.name)
                if not recovery.exists():
                    write(recovery, {'request': evidence['request'], 'spent_tool_turn': turn,
                          'prior_status': evidence['status'], 'processes_verified_dead': True,
                          'outcome_unknown': True, 'usage': None, 'budget_resets': 0,
                          'reason': 'Continue within remaining original teacher slots after relay interruption'})
        for name in ['child.json', 'supervisor_identity.json']:
            if (OUT / name).exists():
                write(OUT / f'{name}.before-scoped-resume-{stamp}', read(OUT / name))
        write(OUT / f'scoped_resume-{stamp}.json', {'amendment': amendment,
              'prior_status': read(OUT / 'run/status.json'), 'total_reserved': reserved + 64,
              'active_reservations': active, 'contract_preserved': True,
              'execution_fix': 'verifier stdin closed; original scripts and accepted results preserved',
              'teacher_threads': 16, 'teacher_relay_concurrency': 1,
              'brew_unstarted_only': '--brew-unstarted-only' in sys.argv,
              'transport_reason': 'Upstream buffering 503 persists at four streams; serialize remaining transport slots'})
        with (OUT / 'supervisor.log').open('a') as log:
            worker_command = [sys.executable, __file__, '--worker']
            if '--brew-unstarted-only' in sys.argv:
                worker_command.append('--brew-unstarted-only')
            child = subprocess.Popen(worker_command, env=env, cwd=BASE,
                                     stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        write(OUT / 'supervisor_identity.json', process_identity(child.pid))
        registration('running', 'official_acp_scoped_resume_starting', child.pid)
        print(json.dumps({'supervisor_pid': child.pid, 'total_reserved': reserved + 64,
                          'extra_attempts': 1, 'task': unit.name, 'phase': 'baseline'}))
