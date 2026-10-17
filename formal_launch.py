"""Reserve and launch exactly the frozen E11 autonomous schedule."""
import argparse
import json
from pathlib import Path
import os
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--root', type=Path, required=True)
    root = parser.parse_args().root.resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    status = json.loads((root / 'status.json').read_text())
    if status.get('status') != 'formal_prepared' or manifest.get('formal_slots') != 267:
        raise ValueError('E11 formal prerequisites are not frozen')
    if (root / 'formal_dispatch.json').exists():
        raise ValueError('E11 formal schedule already dispatched')
    rows = json.loads((root / 'frozen/schedule.json').read_text())
    if len(rows) != 267 or len({(r['task'], r['attempt']) for r in rows}) != 267:
        raise ValueError('Formal schedule is not exactly 89 x 3')
    reservation = root / 'formal/reservations.json'
    reservation.parent.mkdir(parents=True, exist_ok=True)
    reservation.write_text(json.dumps({'reserved_at': time.time(), 'slots': len(rows),
                                       'physical_max': 534, 'no_replacement': True}, indent=2) + '\n')
    env = dict(os.environ)
    env.update(TBENCH_PERSIST_SANDBOXES='0', TBENCH_TENCENT_ENV_FILE='/dev/null',
               E2B_DOMAIN='ap-beijing.tencentags.com', E2B_VALIDATE_API_KEY='false',
               HARBOR_E2B_FS_USER='root', TBENCH_E2B_RELAY_TEMPLATE='sdt-28lb4d9k',
               TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT='1', TBENCH_RELAY_PROVIDER_BASE='https://llm-center.modelbest.co/v1',
               EXPE_LLM_TIMEOUT_SECONDS='1800', TBENCH_E2B_START_CONCURRENCY='4',
               TBENCH_E2B_RPC_CONCURRENCY='12', TB21_TOTAL_WORKER_LIMIT='250',
               TB21_TASK_VERIFIER_CACHES=json.dumps({}))
    log = (root / 'formal.log').open('a')
    proc = subprocess.Popen([os.environ.get('PYTHON', 'python3'), str(root / 'runner.py'), '--worker'],
                            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    record = {'pid': proc.pid, 'started_at': time.time(), 'slots_reserved': len(rows),
              'schedule': str(root / 'frozen/schedule.json'), 'no_restart': True}
    (root / 'formal_dispatch.json').write_text(json.dumps(record, indent=2) + '\n')
    (root / 'status.json').write_text(json.dumps({'status': 'formal_running', **record}, indent=2) + '\n')
    print(json.dumps(record))


if __name__ == '__main__':
    main()
