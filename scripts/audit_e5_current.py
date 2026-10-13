"""Read frozen E5 evidence and write an independent recovery audit."""
import json
from pathlib import Path
from skillexpand import schema as S

ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
source = ROOT / 'tb21-e5-qwen-deepseek-mixed-parallel-20261006-recovery4-task28-noschema'
out = ROOT / 'tb21-e5-20261006-recovery5-audit'
out.mkdir(exist_ok=False)
preflight = json.loads((source / 'recovery_preflight.json').read_text())
rows = [json.loads(line) for line in (source / 'train/predicted_scores.jsonl').read_text().splitlines() if line.strip()]
coverage = {}
accepted = []
for arm in ('base', 'candidate'):
    body = preflight[arm + '_body_hash']
    records = []
    for row in rows:
        key = S.content_hash({'b': 'terminalbench', 'panel': preflight['panel_key'],
                              't': row['task_id'], 'role': 'predicted:' + preflight['protocol_hash'], 'body': body})
        probability = row.get('probability_true')
        if (row.get('cache_key') == key and row.get('protocol_hash') == preflight['protocol_hash']
                and row.get('panel_key') == preflight['panel_key']
                and isinstance(probability, (int, float)) and not isinstance(probability, bool)
                and 0 <= probability <= 1 and type(row.get('predicted_success')) is bool
                and row['predicted_success'] == (probability >= 0.5)
                and isinstance(row.get('reason'), str)):
            records.append(row)
    ids = [row['task_id'] for row in records]
    coverage[arm] = {'records': len(records), 'unique_tasks': len(set(ids)),
                     'duplicates': len(ids) - len(set(ids)),
                     'missing_task_ids': sorted(set(range(89)) - set(ids)),
                     'task65_exact_cache': any(row['task_id'] == 65 for row in records)}
    accepted.extend(records)
report = {'stage': 'E5', 'status': 'needs_attention', 'source_run': str(source),
          'reviewer_coverage': coverage, 'candidate_gate': 'not_executed',
          'reason': 'Task 28 reviewer missing on both arms; last canary failed before first chunk and cleanup hung.',
          'old_runs_read_only': True, 'new_provider_requests': 0,
          'retry2_state_correction': {'recorded_status': 'running', 'pid': 1507268,
                                    'actual_pid_exists': Path('/proc/1507268').exists(),
                                    'log_error': 'L2/test requires a completed cold start'},
          'retry_budget_increased': False}
for name in ('status.json', 'audit.json', 'input_coverage.json', 'cache_reconciliation.json', 'recovery_ledger.json'):
    (out / name).write_text(json.dumps(report, indent=2) + '\n')
(out / 'predicted_scores.reconciled.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in accepted))
(out / 'recovery_summary.md').write_text('# E5 Evidence Audit\n\nBoth arms have 88/89 exact cache records; only task 28 is missing. Task 65 matches on both arms. Gate withheld. Retry2 has no live PID and failed cold-start validation. Historical runs are unchanged. No new provider request or retry budget was added.\n')
print(json.dumps(report))
