"""Resume frozen drivers after offline recovery, without resetting any slots."""
import fcntl
import os
import shutil
import subprocess
import sys
import time
from state import ROOT, TASKS, ARMS, read, write, verify_freeze
from resume_monitor import alive


def main():
    verify_freeze()
    if shutil.disk_usage(ROOT).free < 1024**3:
        raise RuntimeError('Storage is still insufficient after the actual disk failure')
    for row in read(ROOT/'dispatch.json')['drivers']:
        if alive(row['pid']):
            raise RuntimeError('Original driver is still alive')
    audit = read(ROOT/'post_recovery_audit.json')
    if audit['errors']:
        raise RuntimeError('Offline recovery audit has unresolved errors')
    from completion_audit import CompletionAudit
    current = CompletionAudit(ROOT).build()
    if current['errors']:
        raise RuntimeError('Current archive failed pre-continuation audit')
    with (ROOT/'.resume.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (ROOT/'resume_dispatch.json').exists():
            raise RuntimeError('Continuation already dispatched; inspect its original PIDs')
        sys.path.insert(0, str(ROOT.parents[1]/'scripts'))
        from run_tb21_empirical import active_worker_reservation, total_worker_limit
        with (ROOT.parent/'.tb21-worker-reservations.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reserved, active = active_worker_reservation(ROOT.parent, ROOT)
            limit = total_worker_limit()
            if limit != read(ROOT/'manifest.json')['coordination_worker_limit'] or reserved+45 > limit:
                raise RuntimeError('Shared capacity unavailable')
            children = []
            for task in TASKS:
                for arm in ARMS:
                    if (ROOT/'tasks'/task/'arms'/arm/'final_summary.json').exists():
                        continue
                    log = ROOT/'tasks'/task/(arm+'_continuation_driver.log')
                    with log.open('a') as stream:
                        child = subprocess.Popen([sys.executable, str(ROOT/'driver.py'),
                            '--task', task, '--arm', arm], env=os.environ.copy(),
                            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                            start_new_session=True)
                    children.append({'task': task, 'phase': arm, 'pid': child.pid,
                                     'log': str(log.relative_to(ROOT))})
            write(ROOT/'resume_dispatch.json', {'at': time.time(), 'drivers': children,
                'reserved': 45, 'already_reserved': reserved, 'original_dispatch': 'dispatch.json',
                'previous_reservations': current['reservation_audit'],
                'budgets_reset': False, 'retry_consumed_slots': False,
                'frozen_driver_unchanged': True})
            with (ROOT/'resume_monitor.log').open('a') as stream:
                monitor = subprocess.Popen([sys.executable, str(ROOT/'resume_monitor.py')],
                    env=os.environ.copy(), stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.STDOUT, start_new_session=True)
            write(ROOT/'status.json', {'status': 'running', 'workers': 45, 'pid': monitor.pid,
                'stage': 'input_ablation_fixed_protocol_continuation', 'running_drivers': children})
    print(__import__('json').dumps({'continued_drivers': children, 'already_reserved': reserved,
                                  'worker_limit': limit, 'budgets_reset': False}))


if __name__ == '__main__':
    main()
