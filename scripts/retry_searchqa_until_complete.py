#!/usr/bin/env python3
"""Retry only unfinished SearchQA final-evaluation units until completion."""
import os, runpy, subprocess, sys, time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
final_workers = int(sys.argv[2]) if len(sys.argv) > 2 else 128
attempts = int(sys.argv[3]) if len(sys.argv) > 3 else 40
if os.fork():
    raise SystemExit(0)
os.setsid()
campaign = runpy.run_path(str(root/'code/run_campaign.py'))
manifest = campaign['verify'](root)
out = root/'full/searchqa/final-evolve1'
log = root/'full/searchqa/logs/final-evolve1.log'
status = root/'final-evolve1-searchqa-status.json'
for attempt in range(1, attempts+1):
    if (out/'summary.json').exists():
        campaign['save'](status, {'status':'complete','attempts':attempt-1,
                                  'final_workers':final_workers})
        break
    cmd = [manifest['python'], '-u', str(Path(__file__).with_name('evaluate_snapshot.py')),
           '--run-dir', str(root/'full/searchqa/run'), '--round', '1', '--output', str(out),
           '--final-workers', str(final_workers)]
    with log.open('ab') as stream:
        child = subprocess.Popen(cmd, cwd=manifest['repo'], env=campaign['environment'](root),
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True)
        campaign['save'](status, {'status':'running','pid':child.pid,
            'supervisor_pid':os.getpid(),'final_workers':final_workers,'attempt':attempt,
            'max_attempts':attempts})
        rc = child.wait()
    if rc == 0:
        campaign['save'](status, {'status':'complete','pid':child.pid,
            'returncode':0,'final_workers':final_workers,'attempt':attempt})
        break
    time.sleep(3)
else:
    campaign['save'](status, {'status':'needs_attention','final_workers':final_workers,
                              'attempts':attempts})
