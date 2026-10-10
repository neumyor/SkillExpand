"""External Harbor evidence indexed by original task and executed Skill version."""
import json
from pathlib import Path

from skillexpand import schema as S
from skillexpand.benchmarks.terminalbench import audit_harbor_experience

FORMAT = 'task-skill-v1'


def read(path):
    return json.loads(Path(path).read_text())


def experience_id(task_id, skill_key, round_index=1):
    return f'evolution:{round_index}:{task_id}:{skill_key}:experience'


def validate_source(root, task_id, trial, skill, skills, model):
    """Recheck raw evidence and the original request, never trusting card labels."""
    root = Path(root).resolve()
    attempt = trial['source_attempt_index']
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
        raise ValueError('Invalid original attempt identity')
    slot = f'{task_id:02d}-{attempt}'
    request = Path(trial['source_request']).resolve()
    if (trial['source_task_id'] != task_id or trial['source_slot'] != slot
            or request.parent != root / 'slots' / slot / 'requests'):
        raise ValueError('Source slot/request identity mismatch')
    result_path = Path(trial['result_path']).resolve()
    if result_path.parents[3] != request or result_path.name != 'result.json':
        raise ValueError('Result is outside source request')
    result = read(result_path)
    tasks = read(root / 'tasks.json')
    if result.get('task_name') != tasks[task_id]['task_name']:
        raise ValueError('Source task identity mismatch')
    exception = result.get('exception_info') or {}
    if exception and exception.get('exception_type') != 'AgentTimeoutError':
        raise ValueError('Invalid source exception')
    reward = (result.get('verifier_result') or {}).get('rewards', {}).get('reward')
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or reward not in (0, 1):
        raise ValueError('Missing real verifier reward')
    if trial.get('verifier_reward') != reward or trial.get('success') is not bool(reward):
        raise ValueError('Source reward mismatch')
    if (trial.get('phase') != 'autonomous' or trial.get('status') != 'completed'
            or trial.get('termination') != (exception.get('exception_type') or 'verifier')):
        raise ValueError('Source trial execution status mismatch')
    agent = result.get('config', {}).get('agent', {})
    if (agent.get('model_name') != 'openai/' + model.removeprefix('openai/')
            or trial.get('source_model') != model
            or not str(agent.get('import_path', '')).endswith(':TencentSkillTerminus2')):
        raise ValueError('Source executor mismatch')
    trajectory = result_path.parent / 'agent/trajectory.json'
    if Path(trial['trajectory']).resolve() != trajectory or not read(trajectory):
        raise ValueError('Source trajectory mismatch')
    selection = read(request / 'selection.json')
    catalog = [{'skill_id': s.skill_id, 'description': s.description}
               for s in sorted(skills, key=lambda s: s.skill_id)]
    if (trial['selection'] != selection or not selection.get('ok')
            or selection.get('catalog') != catalog
            or selection.get('skill_id') != skill.skill_id
            or selection.get('loaded_skill_id') != skill.skill_id
            or selection.get('loaded_skill_key') != skill.key
            or selection.get('load_stage') != 'after_selection'):
        raise ValueError('Source selected Skill mismatch')
    activation = read(result_path.parent / 'agent/skill_activation.json')
    mounted = Path(activation['source_path']).resolve()
    if (activation.get('load_stage') != 'after_selection'
            or not mounted.is_relative_to(request) or mounted.read_text() != skill.body):
        raise ValueError('Source activation/body mismatch')
    row = read(root / 'slots' / slot / 'record.json')
    if (row.get('status') != 'valid' or row.get('task_id') != task_id
            or row.get('attempt_index') != attempt or row.get('reward') != reward
            or row.get('task_name') != tasks[task_id]['task_name']
            or Path(row['result_path']).resolve() != result_path
            or Path(row['trajectory_path']).resolve() != trajectory
            or Path(row['skill_file']).resolve() != mounted or row['selection'] != selection):
        raise ValueError('Source valid slot record mismatch')
    return slot


def validate_cards(root, manifest, cards, skills, task_ids, *, complete=True):
    """Prove every accepted slot occurs exactly once in its actual Skill's card."""
    if manifest.get('format') != FORMAT or manifest.get('external_harbor') is not True:
        raise ValueError('Unknown external experience format')
    ids = manifest['experience_ids']
    if ids != sorted(set(ids)) or set(ids) != set(cards) or set(ids) != set(manifest['experiences']):
        raise ValueError('External experience identity coverage mismatch')
    if manifest['task_ids'] != sorted(task_ids) or manifest.get('attempts_per_task') != 3:
        raise ValueError('External task coverage mismatch')
    if manifest['skill_keys'] != {s.family_id: s.key for s in skills}:
        raise ValueError('External Skill heads mismatch')
    expected = {f'{t:02d}-{a}' for t in task_ids for a in (1, 2, 3)}
    seen = set()
    by_key = {s.key: s for s in skills}
    for eid in ids:
        exp = cards[eid]
        entry = manifest['experiences'][eid]
        skill = by_key.get(exp.initial_skill_key)
        if (skill is None or exp.task_id not in task_ids or exp.benchmark != 'terminalbench'
                or exp.split != S.SPLIT_TRAIN or exp.evolution_round != manifest['round']
                or eid != experience_id(exp.task_id, skill.key, manifest['round'])
                or exp.experience_id != eid or exp.family_id != skill.family_id
                or exp.selected_skill_id != skill.skill_id or exp.selection_source != S.SELECTION_AGENT
                or exp.skill_load != {'load_stage': 'after_selection', 'skill_key': skill.key,
                                      'skill_id': skill.skill_id}
                or entry['task_id'] != exp.task_id or entry['skill_key'] != skill.key
                or exp.experience_card.get('card_id') != eid.removesuffix(':experience') + ':card'
                or exp.experience_card.get('task', {}).get('task_id') != exp.task_id):
            raise ValueError('External card identity/Skill mismatch')
        if tuple(exp.selection_catalog) != tuple(exp.l1_trials[0]['selection']['catalog']):
            raise ValueError('External card selection catalog mismatch')
        audit_harbor_experience(exp)
        if exp.num_trials != len(exp.l1_trials) or tuple(exp.trial_phases) != ('autonomous',) * len(exp.l1_trials):
            raise ValueError('External trial count/phase mismatch')
        if manifest['cards'][eid] != S.content_hash(S.to_dict(exp)):
            raise ValueError('External card content mismatch')
        own = []
        for trial in exp.l1_trials:
            slot = validate_source(root, exp.task_id, trial, skill, skills, manifest['executor_model'])
            if slot in seen:
                raise ValueError('Duplicate source slot')
            seen.add(slot)
            own.append(slot)
        if entry['slots'] != own or own != sorted(own):
            raise ValueError('External card slot index mismatch')
    if not seen <= expected or (complete and seen != expected):
        raise ValueError('Incomplete original slot coverage; L2 remains locked')
    return {'passed': seen == expected, 'valid_slots': len(seen), 'expected_slots': len(expected),
            'experience_cards': len(cards), 'unique_keys': len(seen),
            'missing_slots': sorted(expected - seen), 'format': FORMAT}


def read_cards(root, round_index, skills, task_ids):
    directory = Path(root) / 'evolution' / f'round-{round_index}'
    manifest = read(directory / 'manifest.json')
    if manifest.get('round') != round_index:
        raise ValueError('External card round mismatch')
    cards = {}
    files = set()
    for eid in manifest['experience_ids']:
        name = manifest['experiences'][eid]['file']
        if Path(name).name != name or not name.endswith('.json') or name in files:
            raise ValueError('Invalid/duplicate external card filename')
        files.add(name)
        cards[eid] = S.from_dict(S.TaskExperience, read(directory / 'cards' / name))
    if {p.name for p in (directory / 'cards').glob('*.json')} != files:
        raise ValueError('Unexpected/missing external card files')
    validate_cards(root, manifest, cards, skills, task_ids)
    return cards
