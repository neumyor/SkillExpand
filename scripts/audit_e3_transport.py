"""Audit E3 production recovery against the frozen streaming protocol."""
import json
from pathlib import Path

root = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
source = root / 'tb21-e3-20261007-recovery7-production-task28'
out = root / 'tb21-e3-20261007-recovery7-protocol-audit'
out.mkdir(exist_ok=False)
result = json.loads((source / 'result.json').read_text())
rows = []
for trial in result['experience']['l1_trials']:
    trajectory = Path(trial['trajectory'])
    request_path = trajectory.parent / 'raw_model_requests.jsonl'
    requests = [json.loads(line) for line in request_path.read_text().splitlines()]
    text = json.dumps(requests)
    raw_result = json.loads((trajectory.parent.parent / 'result.json').read_text())
    rows.append({'attempt_index': trial['index'], 'trajectory_path': str(trajectory),
        'request_path': str(request_path), 'result_path': str(trajectory.parent.parent / 'result.json'),
        'all_requests_streaming': all(r.get('stream') is True for r in requests),
        'explicit_skill_path_in_requests': '/opt/openclaw-skills' in text,
        'skill_body_in_requests': 'Make the smallest repair supported by observed evidence' in text,
        'verifier_result': raw_result.get('verifier_result'), 'exception_info': raw_result.get('exception_info')})
report = {'stage': 'E3', 'status': 'needs_attention', 'source_run': str(source),
    'task_id': 28, 'eligible_for_stage_completion': False, 'old_runs_read_only': True,
    'reason': 'Harbor adapter omitted stream:true; explicit Skill activation lacks request/trajectory evidence. Verifier outcomes are retained but do not satisfy frozen protocol.',
    'trials': rows, 'next_step': 'Repair Tencent Harbor streaming adapter and Skill activation evidence before protocol-compliant recovery'}
for name in ('status.json', 'audit.json', 'result.json', 'manifest.json'):
    (out / name).write_text(json.dumps(report, indent=2) + '\n')
(out / 'recovery_summary.md').write_text('# E3 protocol audit\n\n' + report['reason'] + '\n')
print(json.dumps(report))
