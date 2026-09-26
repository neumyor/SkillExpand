#!/usr/bin/env python3
"""One-shot, resumable provider capacity probe at the campaign's worker mix."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False))
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=0)
    parser.add_argument('--name', default='capacity')
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--stage', choices=(
        'cold_start_workers', 'family_discovery_workers', 'evolve_l1_workers',
        'l2_review_workers', 'final_workers'), default='cold_start_workers')
    args = parser.parse_args()
    if args.interval < 0 or args.rounds < 1 or not args.name.replace('-', '').isalnum():
        parser.error('interval must be nonnegative, rounds positive, and name alphanumeric or hyphenated')
    root = args.root.resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    concurrency = manifest['concurrency']
    workers = {benchmark: settings[args.stage]
               for benchmark, settings in concurrency.items()}
    output = root / 'preflight' / args.name
    units = [(round_id, benchmark, index) for round_id in range(args.rounds)
             for benchmark, count in workers.items() for index in range(count)]
    pending = [(round_id, benchmark, index) for round_id, benchmark, index in units
               if not (output / f'{round_id}-{benchmark}-{index}.json').exists()]
    base_url = os.environ['EXPE_LLM_BASE_URL'].rstrip('/')
    key = os.environ['OPENAI_API_KEY']

    def request(round_id, benchmark, index):
        started = time.monotonic()
        payload = {'model': manifest['model'], 'messages': [
            {'role': 'user', 'content': f'Reply OK. Probe {benchmark} {index}.'}],
            'max_tokens': 8, 'temperature': 0, 'enable_thinking': False}
        req = urllib.request.Request(base_url + '/chat/completions',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                value = json.loads(response.read())
            content = value['choices'][0]['message'].get('content', '')
            result = {'status': 'ok' if content.strip() else 'empty',
                      'reported_model': value.get('model')}
        except urllib.error.HTTPError as exc:
            result = {'status': 'http_error', 'http_status': exc.code,
                      'error': exc.read(500).decode(errors='replace')}
        except Exception as exc:
            result = {'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
        result.update(round_id=round_id, benchmark=benchmark, index=index,
                      seconds=round(time.monotonic() - started, 3))
        save(output / f'{round_id}-{benchmark}-{index}.json', result)
        return result

    with ThreadPoolExecutor(max_workers=len(units)) as pool:
        futures = []
        next_submission = time.monotonic()
        for round_id, benchmark, index in pending:
            if args.interval:
                time.sleep(max(0, next_submission - time.monotonic()))
                next_submission += args.interval
            futures.append(pool.submit(request, round_id, benchmark, index))
        for future in as_completed(futures):
            future.result()
    results = [json.loads((output / f'{round_id}-{benchmark}-{index}.json').read_text())
               for round_id, benchmark, index in units]
    summary = {'stage': args.stage, 'workers': workers, 'interval': args.interval,
               'rounds': args.rounds, 'total': len(results),
               'ok': sum(row['status'] == 'ok' for row in results),
               'http_429': sum(row.get('http_status') == 429 for row in results),
               'other_failures': sum(row['status'] != 'ok' and row.get('http_status') != 429
                                     for row in results),
               'max_seconds': max(row['seconds'] for row in results)}
    save(output / 'summary.json', summary)
    print(json.dumps(summary))
    return 0 if summary['ok'] == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
