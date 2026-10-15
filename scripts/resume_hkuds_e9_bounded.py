"""Launch the 2026-10-15 bounded recovery under E9's shared capacity lock."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

from hkuds_e9_recovery import (REPO, BASE, create_amendment, create_ten_turn_amendment,
                               create_length_continuation)

OUT = BASE / 'outputs/E9_hkuds_upskill_acp_evo89_test89_20261013'
REG = REPO / 'runs/tb21-e9-agentbrew-evo89-test89-20261012'
sys.path[:0] = [str(BASE / 'src'), str(REPO / 'src')]
from tbench_eval.agentbrew.storage import read, save
from tbench_eval.agentbrew.processes import process_identity, process_matches, resume_readiness


def command(*extra):
    return [str(BASE / '.venv/bin/python'), str(REPO / 'scripts/run_hkuds_upskill_tb21.py'),
            '--source', str(REPO / 'sources/HKUDS-UpSkill'), '--root', str(OUT / 'run'),
            '--eval-config', str(BASE / 'configs/agentbrew_tb21_evo89_test89_eval.yaml'),
            '--student-workers', '64', '--teacher-workers', '1', *extra]


def registration(status, phase, **extra):
    save(REG / 'status.json', {'stage': 'E9', 'status': status, 'phase': phase,
         'pid': os.getpid(), 'workers': 64 if status == 'running' else 0,
         'output_root': str(OUT), **extra})


def main():
    after_length = '--continue-after-length' in sys.argv
    ten_turn = '--ten-turn-recovery' in sys.argv or after_length
    revision = ('after_length_recovery_20261015' if after_length else
                'ten_turn_recovery_20261015' if ten_turn else 'bounded_recovery_20261015')
    if '--worker' in sys.argv:
        try:
            with (OUT / (revision + '.log')).open('a') as log:
                child = subprocess.Popen(command('--bounded-recovery'), cwd=BASE,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
            save(OUT / 'child.json', {**process_identity(child.pid),
                'phase': revision, 'command': command('--bounded-recovery')})
            registration('running', revision, driver_pid=child.pid)
            code = child.wait()
            registration('needs_audit' if code == 0 else 'needs_attention',
                         'bounded_recovery_finished', exit_code=code)
        except Exception:
            registration('needs_attention', 'bounded_recovery_launcher_failed')
            raise
        return
    if '--preflight' in sys.argv:
        raise SystemExit(subprocess.call(command('--recovery-preflight'), cwd=BASE))
    if '--launch' not in sys.argv:
        raise SystemExit('Specify --preflight or --launch')
    from run_tb21_empirical import active_worker_reservation
    with (REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ['child.json', 'supervisor_identity.json']:
            if (OUT / name).exists() and process_matches(read(OUT / name)):
                raise SystemExit('Live E9 identity blocks recovery')
        for directory in [OUT / 'run', OUT / 'smoke',
                BASE / 'outputs/E9_agentbrew_tb21_evo89_test89/run']:
            if directory.exists() and not resume_readiness(directory)['safe_to_open_run']:
                raise SystemExit('Live E9 rollout blocks recovery')
        for proc in Path('/proc').glob('[0-9]*'):
            if int(proc.name) == os.getpid():
                continue
            try:
                argv = proc.joinpath('cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeError):
                continue
            if '-c' not in argv and any(str(OUT) in arg for arg in argv):
                raise SystemExit('Unregistered E9 process blocks recovery')
        reserved, active = active_worker_reservation(REPO / 'runs', REG)
        if reserved + 64 > 250:
            raise SystemExit('Insufficient shared worker capacity')
        if read(OUT / 'smoke/audit.json').get('complete') is not True:
            raise SystemExit('Original smoke is not accepted')
        env = dict(os.environ)
        env.pop('TB21_AUTHORIZED_INFRA_EXTRA_ATTEMPTS', None)
        env.update(PYTHONPATH=':'.join([str(BASE / 'src'), str(REPO / 'src')]),
            TBENCH_LLM_KEY=env['TB21_EXECUTOR_API_KEY'], OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'],
            HARBOR_E2B_FS_USER='root', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
            TBENCH_RELAY_MAX_CONCURRENCY='1')
        # No marker or amendment is created until the zero-model preflight succeeds.
        subprocess.run(command('--recovery-preflight'), cwd=BASE, env=env, check=True)
        create_amendment(OUT / 'run', save)
        launch_path = OUT / (revision.replace('_20261015', '_launch_20261015') + '.json')
        if launch_path.exists():
            raise SystemExit('Bounded recovery already dispatched; inspect failure before any restart')
        if after_length:
            if not (OUT / 'ten_turn_recovery_launch_20261015.json').exists():
                raise SystemExit('Post-length continuation requires the ten-turn launch')
            create_length_continuation(OUT / 'run', save)
        elif ten_turn:
            if not (OUT / 'bounded_recovery_launch_20261015.json').exists():
                raise SystemExit('Ten-turn continuation requires the original recovery launch')
            create_ten_turn_amendment(OUT / 'run', save)
        save(launch_path, {
            'authorization': ('user_continue_e9_after_length_20261015' if after_length else
                              'user_approved_ten_turn_resume_20261015' if ten_turn
                              else 'user_approved_plan_20261015'), 'reserved': reserved + 64,
            'teacher_turn_limit': 10 if ten_turn else 8,
            'active_reservations': active, 'budget_resets': 0})
        for name in ['child.json', 'supervisor_identity.json']:
            if (OUT / name).exists():
                save(OUT / ('before-' + revision + '-' + name), read(OUT / name))
        with (OUT / 'supervisor.log').open('a') as log:
            child = subprocess.Popen([sys.executable, __file__, '--worker',
                *(['--continue-after-length'] if after_length else
                  ['--ten-turn-recovery'] if ten_turn else [])], cwd=BASE,
                env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        save(OUT / 'supervisor_identity.json', process_identity(child.pid))
        save(REG / 'status.json', {'stage': 'E9', 'status': 'running', 'phase': 'bounded_recovery_starting',
            'pid': child.pid, 'workers': 64, 'output_root': str(OUT), 'recovery_revision': revision})
        print(json.dumps({'supervisor_pid': child.pid, 'total_reserved': reserved + 64}))


if __name__ == '__main__':
    main()
