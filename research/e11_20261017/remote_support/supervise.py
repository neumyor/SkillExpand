import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def write(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


write('status.json', {'status': 'running', 'formal_reserved': 0, 'started_at': time.time()})
command = [sys.executable, str(ROOT / 'driver.py'), 'evolve', '--root', str(ROOT)]
try:
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    write('driver_process.json', {'pid': child.pid, 'command': command, 'no_restart': True})
    code = child.wait()
    failure = None
except Exception as exc:
    code = -1
    failure = f'{type(exc).__name__}: {exc}'
requests = list((ROOT / 'generation_requests').glob('*-*.json'))
requests = [path for path in requests if not path.name.endswith('.response.json')]
responses = list((ROOT / 'generation_requests').glob('*.response.json'))
result = {
    'status': 'generation_complete_pending_review' if code == 0 else 'failed',
    'exit_code': code, 'finished_at': time.time(), 'formal_reserved': 0,
    'request_count': len(requests), 'response_count': len(responses),
    'no_restart': True,
}
if failure:
    result['supervisor_error'] = failure
write('process_result.json', result)
write('status.json', result)
raise SystemExit(code if code >= 0 else 1)
