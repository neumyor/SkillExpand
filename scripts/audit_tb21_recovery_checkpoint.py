"""Audit production card parsing and remaining frozen candidate coverage."""
import json
from pathlib import Path
from skillexpand import schema as S
from skillexpand.evaluation.validation import ScoreCache


root = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
e5 = root / 'tb21-e5-20261006-recovery7-task28-stream'
source = root / 'tb21-e5-qwen-deepseek-mixed-parallel-20261006-recovery3-candidate7'
pre = json.loads((root / 'tb21-e5-qwen-deepseek-mixed-parallel-20261006-recovery4-task28-noschema/recovery_preflight.json').read_text())
records = [json.loads(line) for line in (source / 'train/predicted_scores.jsonl').read_text().splitlines()]
records += [json.loads(line) for line in (e5 / 'train/predicted_scores.jsonl').read_text().splitlines()]
keys = {record['cache_key'] for record in records}
coverage = []
for path in sorted((source / 'l2_proposals').glob('*/candidate-*.json')):
    candidate = json.loads(path.read_text())['candidate']
    body = candidate['skill']['body']
    missing = [task for task in range(89) if ScoreCache.make_key(
        'terminalbench', pre['panel_key'], task, 'predicted:' + pre['protocol_hash'], body) not in keys]
    coverage.append({'proposal_path': str(path), 'candidate_id': candidate['candidate_id'],
                     'skill_body_hash': S.content_hash(body), 'exact_cache_tasks': 89 - len(missing),
                     'missing_task_ids': missing})
report = {'status': 'needs_attention', 'candidate_coverage': coverage,
          'reason': 'Only one frozen candidate panel recovered; original multi-candidate selection, batch journals and stage audit remain',
          'candidate_committed': False, 'old_runs_read_only': True}
(e5 / 'remaining_candidate_coverage.json').write_text(json.dumps(report, indent=2) + '\n')
(e5 / 'recovery_summary.md').write_text(
    '# E5 Recovery Checkpoint\n\nTask 28 reviewer recovery uses the original prompt, model, strict streaming and parser. '
    'Task 65 exact cache is reused on both arms. See validation.json for the current candidate gate. '
    'The stage remains needs_attention because remaining frozen candidate panels and original batch journals are incomplete. '
    'No candidate is committed by this recovery.\n')
e3 = root / 'tb21-e3-deepseek-parallel-20261006-recovery5'
card = e3 / 'evolution/round-1/cards/28.json'
diagnostic = {'stage': 'E3', 'source_run': str(e3), 'recorded_status': 'complete', 'card_path': str(card),
              'old_run_read_only': True, 'new_provider_requests': 0}
try:
    S.from_dict(S.TaskExperience, json.loads(card.read_text()))
    diagnostic.update(status='card_schema_passed')
except Exception as exc:
    diagnostic.update(status='needs_attention', error_class='card_schema_invalid', error=str(exc),
                      reason='Serialized card payload cannot replace the production TaskExperience artifact')
out = root / 'tb21-e3-20261006-card28-schema-audit'
out.mkdir(exist_ok=False)
for name in ('status.json', 'audit.json', 'result.json'):
    (out / name).write_text(json.dumps(diagnostic, indent=2) + '\n')
(out / 'recovery_summary.md').write_text('# E3 Card Audit\n\n' + diagnostic.get('reason', diagnostic['status']) + '\n')
print(json.dumps({'E3': diagnostic, 'E5': report}))
