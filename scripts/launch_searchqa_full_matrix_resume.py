#!/usr/bin/env python3
"""Run the frozen full campaign for SearchQA only, one condition at a time.

This deliberately bypasses the two-benchmark supervisor. It reuses each
condition's frozen ``code/run_campaign.py`` and its resumable SearchQA job,
while leaving ALFWorld deferred and avoiding a misleading full completion file.
"""
import json
import os
import runpy
import sys
import time
from pathlib import Path


CONDITIONS = (
    'baseline',
    'l1_executor-strong',
    'cold_start-strong',
    'l2_planner-strong',
    'l2_reviewer-strong',
)


def atomic_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    os.replace(temp, path)


def main():
    matrix = Path(sys.argv[1]).resolve()
    if os.getsid(0) != os.getpid():
        os.setsid()
    status_path = matrix / 'searchqa-resume-status.json'
    status = {
        'status': 'running',
        'benchmark': 'searchqa',
        'conditions': list(CONDITIONS),
        'started': time.time(),
        'current': None,
        'results': {},
    }
    atomic_save(status_path, status)
    for condition in CONDITIONS:
        root = matrix / condition
        campaign = runpy.run_path(str(root / 'code' / 'run_campaign.py'))
        status['current'] = condition
        status['results'][condition] = {'status': 'health_check'}
        atomic_save(status_path, status)
        campaign['health'](root)
        status['results'][condition] = {'status': 'running', 'started': time.time()}
        atomic_save(status_path, status)
        rc = campaign['run_job'](root, 'full', 'searchqa')
        result = {'status': 'complete' if rc == 0 else 'needs_attention',
                  'returncode': rc, 'finished': time.time()}
        status['results'][condition] = result
        atomic_save(status_path, status)
        if rc != 0:
            status.update(status='needs_attention', finished=time.time())
            atomic_save(status_path, status)
            return rc
    status.update(status='searchqa_complete', current=None, finished=time.time(),
                  deferred_benchmarks=['alfworld'])
    atomic_save(status_path, status)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
