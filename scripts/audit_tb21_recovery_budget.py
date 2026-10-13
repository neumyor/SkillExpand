"""Read trial histories and freeze a conservative E7/E8 recovery ledger."""
import argparse
import json
from pathlib import Path


def e8_rows(job, archive, rejected, recovery=None):
    config = json.loads((job / 'config.json').read_text())
    limit = 1 + config['orchestrator']['retry']['max_retries']
    rows = []
    for row in rejected:
        saved = list((archive / job.name / row['trial']).glob('execution-*'))
        terminal_exception = row.get('exception') is not None
        # Hooks archive every exception, including the final execution.
        used = len(saved) + (0 if terminal_exception else 1)
        recovery_intent = recovery / row['trial'] / 'recovery_intent.json' if recovery else None
        if recovery_intent and recovery_intent.exists():
            used += 1
        if not saved and terminal_exception:
            reason, remaining = 'retry_history_missing', 0
        else:
            reason, remaining = ('budget_exhausted' if used >= limit else 'within_budget'), max(0, limit - used)
        rows.append({'trial': row['trial'], 'task': row['task'], 'executions_used': used,
                     'execution_limit': limit, 'remaining': remaining, 'reason': reason,
                     'snapshot_available': False})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--jobs', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    e7 = args.repo / 'runs/tb21-qwen-deepseek-library-qwen-selector-20261011/revisions/178-cards-v1'
    e8 = args.repo / 'runs/tb21-e8-deepseek-max-1m-20261012'
    rows = []
    for slot in ['51-2', '73-1', '73-2', '73-3', '77-3']:
        requests = sorted((e7 / 'slots' / slot / 'requests').iterdir())
        # 77-3 is the already authorized 004-006 batch; other slots retain 001-003.
        limit = 6 if slot == '77-3' else 3
        active = [p.name for p in requests if not list(p.glob('jobs/*/*/result.json'))
                  and not (p / 'error.json').exists()]
        record = json.loads((e7 / 'slots' / slot / 'record.json').read_text())
        accepted = False
        if record.get('status') == 'valid':
            from summarize_tb21_empirical import validate_trial
            try:
                validate_trial(record['result_path'], record['task_name'], activation=True)
                accepted = True
            except (ValueError, OSError, KeyError):
                pass
        rows.append({'slot': slot, 'requests_used': len(requests), 'request_limit': limit,
                     'remaining': 0 if accepted else max(0, limit - len(requests)), 'active_requests': active,
                     'accepted': accepted,
                     'reason': 'accepted_no_dispatch' if accepted else 'active_request' if active else 'budget_exhausted' if len(requests) >= limit else 'within_budget'})
    job = args.jobs / 'tb21_e8_deepseek_max_1m_raw_20261012'
    audit = json.loads((e8 / 'execution_audit.json').read_text())
    payload = {'E7': rows, 'E8': e8_rows(job, args.jobs / 'e8_retry_archives', audit['rejected'],
                                     e8 / 'infrastructure_recovery_20261013'),
               'policy': 'No new launch resets cumulative request or execution counts; missing history blocks dispatch.'}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + '\n')
    print(json.dumps({'E7_remaining': sum(r['remaining'] for r in rows),
                      'E8_remaining': sum(r['remaining'] for r in payload['E8'])}))


if __name__ == '__main__':
    main()
