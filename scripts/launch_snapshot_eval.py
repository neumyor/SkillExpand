#!/usr/bin/env python3
"""Detached parallel evaluator for evolve-1 test snapshots."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import runpy
import json


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--test-workers', type=int, default=256)
    p.add_argument('--searchqa-test-workers', type=int)
    p.add_argument('--alfworld-test-workers', type=int)
    args = p.parse_args()
    root = args.run_dir.resolve()
    campaign = runpy.run_path(str(root / 'code/run_campaign.py'))
    manifest = campaign['verify'](root)
    test_workers = {b: getattr(args, b + '_test_workers') or args.test_workers
                     for b in ('searchqa', 'alfworld')}
    if any(not 1 <= n <= 256 for n in test_workers.values()):
        raise ValueError('Test worker counts must be between 1 and 256')
    with campaign['locked'](root / 'test-evolve1-launch.lock'):
        return launch(root, test_workers, campaign, manifest)


def launch(root, test_workers, campaign, manifest):
    jobs = []
    for benchmark in ('searchqa', 'alfworld'):
        run = root / 'full' / benchmark / 'run'
        output = root / 'full' / benchmark / 'test-evolve1'
        log = root / 'full' / benchmark / 'logs' / 'test-evolve1.log'
        output.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        command = [manifest['python'], '-u', str(Path(__file__).resolve().parent / 'evaluate_snapshot.py'),
                   '--run-dir', str(run), '--round', '1', '--output', str(output),
                   '--test-workers', str(test_workers[benchmark])]
        stream = log.open('ab')
        jobs.append((benchmark, subprocess.Popen(command, cwd=manifest['repo'], env=campaign['environment'](root),
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True), stream))
    status = {b: {'pid': c.pid, 'returncode': None,
                  'test_workers': test_workers[b]} for b,c,_ in jobs}
    campaign['save'](root / 'test-evolve1-status.json', {'status': 'running', 'jobs': status})
    for benchmark, child, stream in jobs:
        status[benchmark]['returncode'] = child.wait()
        campaign['save'](root / 'test-evolve1-status.json', {'status': 'running', 'jobs': status})
        stream.close()
    campaign['save'](root / 'test-evolve1-status.json', {'status': 'complete' if all(x['returncode'] == 0 for x in status.values()) else 'needs_attention', 'jobs': status})
    print(status)
    raise SystemExit(0 if all(item['returncode'] == 0 for item in status.values()) else 1)


if __name__ == '__main__':
    import sys
    main()
