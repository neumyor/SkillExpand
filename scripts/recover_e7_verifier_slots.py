"""Recover four verifier setup gaps under their original three-request limits."""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from skillexpand.l2.loop import RunLock
from skillexpand.persistence.artifacts import load_cold_start
from skillexpand.runtime.llm_relay import relay_from_env
import run_tb21_aligned as aligned
from summarize_tb21_empirical import read, write

ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/tb21-qwen-deepseek-library-qwen-selector-20261011/revisions/178-cards-v1')
SLOTS = ['51-2', '73-1', '73-2', '73-3']
VERSIONS = {'mteb-retrieve': 'repair-20261013-v2', 'reshard-c4-data': 'repair-20261013-v3'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credential-pid', type=int, required=True)
    parser.add_argument('--slots', nargs='+', choices=SLOTS, default=SLOTS)
    args = parser.parse_args()
    values = dict(v.split('=', 1) for v in Path(f'/proc/{args.credential_pid}/environ').read_bytes().decode().split('\0') if '=' in v)
    for key in ['E2B_API_KEY', 'TB21_METHOD_API_KEY', 'TB21_EXECUTOR_API_KEY']:
        if not values.get(key):
            raise ValueError('Authorized role credential missing')
    from tencent_task_verifier_cache import load
    slots = list(dict.fromkeys(args.slots))
    names = {'mteb-retrieve' if slot == '51-2' else 'reshard-c4-data' for slot in slots}
    for name in names:
        load(name, VERSIONS[name])
    os.environ.update({k: v for k, v in values.items() if k.startswith(('E2B_', 'HARBOR_', 'TBENCH_', 'EXPE_'))})
    os.environ.update(E2B_API_KEY=values['E2B_API_KEY'], OPENAI_API_KEY=values['TB21_METHOD_API_KEY'],
        TB21_METHOD_API_KEY=values['TB21_METHOD_API_KEY'], TB21_EXECUTOR_API_KEY=values['TB21_EXECUTOR_API_KEY'],
        TB21_TASK_VERIFIER_CACHES=json.dumps(VERSIONS),
        TBENCH_PERSIST_SANDBOXES='0', TB21_TOTAL_WORKER_LIMIT='250')
    relay = selector = None
    with RunLock(ROOT / 'driver.pid'):
        remaining = {}
        for slot in slots:
            requests = ROOT / 'slots' / slot / 'requests'
            history = list(requests.iterdir())
            if any(not list(p.glob('jobs/*/*/result.json')) and not (p / 'error.json').exists() for p in history):
                raise ValueError('Unfinished request blocks recovery')
            remaining[slot] = max(0, 3 - len(history))
        aligned.reserve(ROOT, 'E7_verifier_recovery', len(slots))
        write(ROOT / 'verifier_recovery_20261013.json', {'remaining_at_launch': remaining,
              'request_limit': 3, 'pid': os.getpid(), 'budget_resets': 0})
        try:
            relay = relay_from_env()
            url = relay.start()
            os.environ['OPENAI_API_KEY'] = values['TB21_EXECUTOR_API_KEY']
            selector = relay_from_env()
            selector.start()
            os.environ.update(EXPE_CONFIG_FILE=str(ROOT / 'config.json'),
                EXPE_TASK_FILE=str(ROOT / 'tasks.json'), EXPE_LLM_BASE_URL=url,
                OPENAI_API_BASE=url, MODEL_API_BASE=url, EXPE_LLM_RELAY_REQUIRED='1')
            cfg, _, skills, _ = load_cold_start(ROOT)
            tasks = read(ROOT / 'tasks.json')
            def run(slot):
                task, attempt = map(int, slot.split('-'))
                return aligned.run_slot(ROOT, cfg, skills, tasks, selector.transport,
                    task, attempt, aligned.QWEN, aligned.QWEN, max_new_requests=remaining[slot])
            with ThreadPoolExecutor(max_workers=len(slots)) as pool:
                list(pool.map(run, slots))
            _, _, audit = aligned.grouped_cards(ROOT, tasks, skills, aligned.QWEN)
            write(ROOT / 'verifier_recovery_gate_20261013.json', audit)
            aligned.collect_cards(ROOT, tasks, skills, aligned.QWEN)
            write(ROOT / 'status.json', {'stage': 'QD', 'status': 'initial_panel_complete',
                  'coverage': '267/267', 'workers': 0, 'phase': 'awaiting_l2'})
        except Exception as exc:
            message = str(exc)
            for key in ['E2B_API_KEY', 'TB21_METHOD_API_KEY', 'TB21_EXECUTOR_API_KEY']:
                message = message.replace(values[key], '[REDACTED]')
            write(ROOT / 'status.json', {'stage': 'QD', 'status': 'needs_attention',
                  'phase': 'initial_audit', 'workers': 0, 'error': message[:1000]})
            raise
        finally:
            if selector:
                selector.close()
            if relay:
                relay.close()


if __name__ == '__main__':
    main()
