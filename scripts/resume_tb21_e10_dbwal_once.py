"""User-authorized single fresh dispatch for the sole remaining E10 slot."""
import asyncio
import fcntl
import json
import sys

import resume_tb21_e10_apt_missing as recovery

base = recovery.base
recovery.PRIOR = base.REG / 'recovery_apt_staged_20261016'
recovery.REC = base.REG / 'recovery_dbwal_once_20261016'
recovery.JOB_NAME = base.original.FULL + '_dbwal_once_20261016'
recovery.JOB = base.original.ROOT / recovery.JOB_NAME
recovery.EXPECTED_PRIOR_RESULTS = 4
recovery.AUTHORIZATION = 'User in audit conversation: dbwal再试一次; exactly one fresh dispatch, trial retry0, no automatic recovery'
base.REC, base.JOB_NAME, base.JOB = recovery.REC, recovery.JOB_NAME, recovery.JOB
base.BASE_REPORT = recovery.PRIOR / 'reconciled_report.json'
base.__file__ = __file__
recovery.__file__ = __file__
base.COUNT = base.WORKERS = 1


def selection():
    report = base.read(base.BASE_REPORT)
    rows = [r for r in report['rows'] if r['score'] is None]
    assert len(rows) == 1 and rows[0]['trial'] == 'db-wal-recovery__m77VawV'
    assert rows[0]['task'] == 'db-wal-recovery'
    previous = [r for r in report['recovery_rows'] if r['trial'] == rows[0]['trial']]
    assert len(previous) == 1 and previous[0]['exception'] == 'TimeoutException'
    assert previous[0]['reward'] is None and previous[0]['requests'] > 0
    return rows


base.selection = recovery.selection = selection
apt_status = base.status
def status(state, phase, **extra):
    apt_status(state, phase.replace('setup_timeout2_recovery', 'dbwal_once_recovery'), **extra)
base.status = status

if __name__ == '__main__':
    if '--execute' in sys.argv:
        asyncio.run(base.execute())
    elif '--worker' in sys.argv:
        with (base.original.REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        base.worker()
    elif '--audit' in sys.argv:
        result = base.reconcile()
        print(json.dumps({k:v for k,v in result.items() if k not in ['rows','recovery_rows']}))
    elif '--preflight' in sys.argv:
        rows, cfg = recovery.preflight()
        print(json.dumps({'selected': [r['trial'] for r in rows], 'model_requests': 0,
            'retry': cfg['orchestrator']['retry']['max_retries'],
            'environment': cfg['environment'], 'agents': cfg['agents']}))
    else:
        recovery.launch()
