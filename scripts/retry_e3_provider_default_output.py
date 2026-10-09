"""One explicitly authorized extra streamed probe per missing E3 base task."""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    source, out = args.source.resolve(), args.out.resolve()
    out.mkdir(exist_ok=False)
    save(out / 'manifest.json', {'source': str(source), 'task_ids': [20, 71, 81],
        'model': 'deepseek-v4-flash-0731-tencent', 'enable_thinking': True,
        'max_tokens_sent': False, 'response_format_sent': False, 'stream': True,
        'requests_per_task': 1, 'prior_format_budget_exhausted': True,
        'authorization': 'user requested thinking on, omit max length, stream, retry',
        'formal_cache_imported': False, 'old_runs_read_only': True,
        'scope': 'provider-default output retry; not stage completion'})
    save(out / 'status.json', {'status': 'running', 'pid': os.getpid()})
    (out / 'PID').write_text(str(os.getpid()) + '\n')
    def one(task):
        usage = list((source / 'train/usage').glob(f'predicted-*-{task}-*.requests.jsonl'))
        assert len(usage) == 1
        command = [sys.executable, '-u', str(Path(__file__).with_name('diagnose_empty_reviewer.py')),
            '--usage', str(usage[0]), '--out', str(out / f'task-{task}'),
            '--last-start', '--thinking-only', '--omit-max-tokens']
        with (out / f'task-{task}.log').open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        path = out / f'task-{task}/thinking.result.json'
        report = json.loads(path.read_text()) if path.exists() else {
            'status': 'error', 'exit_code': result.returncode}
        return {'task_id': task, **report}
    with ThreadPoolExecutor(max_workers=3) as pool:
        reports = list(pool.map(one, (20, 71, 81)))
    save(out / 'result.json', reports)
    save(out / 'status.json', {'status': 'requests_complete',
        'valid_json_tasks': [r['task_id'] for r in reports if r.get('parsed_content')],
        'failed_tasks': [r['task_id'] for r in reports if not r.get('parsed_content')],
        'formal_stage_complete': False})
    print(json.dumps([{k: r.get(k) for k in ('task_id', 'status', 'content_chars',
        'reasoning_chars', 'finish_reasons', 'parse_error', 'error_class')} for r in reports]))


if __name__ == '__main__':
    main()
