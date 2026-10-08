"""Materialize a schema-valid E3 task-28 experience in a new recovery run."""
import json
import shutil
from pathlib import Path
from skillexpand import schema as S

ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
SRC = ROOT / 'tb21-e3-deepseek-parallel-20261006-recovery5'
OUT = ROOT / 'tb21-e3-20261007-recovery6-task28-schema'
card = json.loads((SRC / 'evolution/round-1/cards/28.json').read_text())
task = card['task']
execution = card.get('execution', {})
trials = execution.get('trials', [])
trial_rewards = tuple(bool(x.get('success')) for x in trials)
trial_phases = tuple(x.get('phase', 'autonomous') for x in trials)
exp = S.TaskExperience(
    experience_id='evolution:1:terminalbench.general:28',
    benchmark=task['benchmark'], task_id=28, task=task['text'],
    family_id=task['family_id'], split='train', reward=any(trial_rewards),
    num_trials=len(trials), initial_skill_key='terminalbench.terminalbench.general@v0',
    initial_meta_skill_version=0, failed_trajectories=tuple(),
    reflections=tuple(), final_trajectory=None,
    selected_skill_id='terminalbench.terminalbench.general',
    selection_source='agent', selection_reason='Recovered from frozen selector/card evidence',
    selection_raw='', selection_catalog=tuple(),
    skill_load={'skill_id': 'terminalbench.terminalbench.general', 'source': 'recovery5'},
    trial_rewards=trial_rewards, trial_phases=trial_phases,
    experience_card=card, l1_audit_path=str(SRC / 'evolution/round-1/cards/28.json'),
    l1_trials=tuple(trials), evolution_round=1,
)
assert S.from_dict(S.TaskExperience, S.to_dict(exp)) == exp
OUT.mkdir(exist_ok=False)
(OUT / 'task_experience.json').write_text(json.dumps(S.to_dict(exp), indent=2) + '\n')
shutil.copy2(SRC / 'evolution/round-1/cards/28.json', OUT / 'source_card.json')
shutil.copy2(SRC / 'recovery_evidence/task28/provenance.json', OUT / 'source_provenance.json')
report = {
    'stage': 'E3', 'status': 'needs_attention', 'source_run': str(SRC),
    'recovery_run': str(OUT), 'task_id': 28, 'schema_valid': True,
    'old_runs_read_only': True,
    'quarantined': True, 'eligible_for_merge': False,
    'reason': 'Diagnostic wrapper only: selector, Skill load and full trial provenance are not established. This artifact must not enter the production library or stage acceptance.',
    'provenance_paths': [str(OUT / 'source_provenance.json'), str(OUT / 'source_card.json')],
}
for name in ('status.json', 'audit.json', 'result.json'):
    (OUT / name).write_text(json.dumps(report, indent=2) + '\n')
(OUT / 'recovery_summary.md').write_text('# E3 task 28 diagnostic draft\n\nQuarantined and ineligible for merge. Schema parsing alone does not establish selector, Skill loading or full trial provenance. Stage remains needs_attention.\n')
print(json.dumps(report))
