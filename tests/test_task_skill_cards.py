"""Offline external experience grouping, provenance, and L2 consumption."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_tb21_aligned as aligned
from skillexpand import schema as S
from skillexpand.benchmarks import task_skill as TS
from skillexpand.l2.loop import SerialEvolutionLoop, EvolutionConfig, LoopPaths
from skillexpand.l2 import audit as A


@pytest.fixture
def sources(tmp_path):
    tasks = [{'task_name': f'task{t}', 'instruction': f'instruction{t}'} for t in range(2)]
    skills = [S.Skill(f'terminalbench.family-p{i:03d}', f'family-p{i:03d}', 0,
                      'generated', f'routing{i}', f'rules{i}') for i in (3, 17, 20)]
    aligned.write(tmp_path / 'tasks.json', tasks)
    catalog = [{'skill_id': s.skill_id, 'description': s.description} for s in skills]
    for task in range(2):
        for attempt in (1, 2, 3):
            skill = skills[1] if task == 0 and attempt == 2 else skills[0]
            slot = tmp_path / 'slots' / f'{task:02d}-{attempt}'
            request = slot / 'requests/001'
            result = request / 'jobs/job/trial/result.json'
            trajectory = result.parent / 'agent/trajectory.json'
            body = request / 'skills/task/SKILL.md'
            body.parent.mkdir(parents=True)
            body.write_text(skill.body)
            selection = {'ok': True, 'catalog': catalog, 'skill_id': skill.skill_id,
                         'loaded_skill_id': skill.skill_id, 'loaded_skill_key': skill.key,
                         'load_stage': 'after_selection', 'why': f'choice-{attempt}', 'raw': f'raw-{attempt}'}
            aligned.write(request / 'selection.json', selection)
            aligned.write(result, {'task_name': tasks[task]['task_name'],
                'config': {'agent': {'model_name': 'openai/' + aligned.METHOD,
                                    'import_path': 'adapter:TencentSkillTerminus2'}},
                'verifier_result': {'rewards': {'reward': attempt % 2}}})
            aligned.write(trajectory, {'steps': [{'message': f't{task}-a{attempt}'}]})
            aligned.write(result.parent / 'agent/skill_activation.json',
                          {'load_stage': 'after_selection', 'source_path': str(body)})
            aligned.write(slot / 'record.json', {'task_id': task, 'attempt_index': attempt,
                'task_name': tasks[task]['task_name'], 'status': 'valid', 'reward': attempt % 2,
                'result_path': str(result), 'trajectory_path': str(trajectory),
                'skill_file': str(body), 'selection': selection})
    return tmp_path, tasks, skills


def driver(root, skills):
    loop = object.__new__(SerialEvolutionLoop)
    loop.paths = LoopPaths(root)
    loop.plan = SimpleNamespace(tasks_in=lambda split: (0, 1), benchmark='terminalbench')
    loop.config = EvolutionConfig(progressive_library=True, external_experience_format=TS.FORMAT)
    loop._round_input = lambda round_index: skills
    loop.skill_heads = lambda: skills
    loop.skills = SimpleNamespace(head=lambda family: next(s for s in skills if s.family_id == family))
    return loop


def test_grouping_preserves_attempts_and_batches_by_executed_skill(sources):
    root, tasks, skills = sources
    cards, manifest, audit = aligned.grouped_cards(root, tasks, skills, aligned.METHOD)
    assert audit['passed'] and audit['valid_slots'] == 6 and len(cards) == 3
    exp = cards[TS.experience_id(0, skills[0].key)]
    assert [t['index'] for t in exp.l1_trials] == [1, 2]
    assert [t['source_attempt_index'] for t in exp.l1_trials] == [1, 3]
    assert [t['selection']['why'] for t in exp.l1_trials] == ['choice-1', 'choice-3']
    assert exp.task_id == 0 and exp.selection_raw == exp.selection_reason == ''
    assert len(cards[TS.experience_id(1, skills[0].key)].l1_trials) == 3
    loop = driver(root, skills)
    batches = loop._evolution_batches(1, cards)
    assert len(batches) == 2  # The unused library Skill is retained without an update.
    first = next(b for b in batches if b['family_id'] == skills[0].family_id)
    assert first['task_ids'] == [0, 1] and len(first['experience_ids']) == 2
    assert sorted(eid for b in batches for eid in b['experience_ids']) == sorted(cards)
    assert loop._evolution_batches(1, cards) == batches


def test_publication_is_idempotent_and_preserves_old_cards(sources):
    root, tasks, skills = sources
    aligned.write(root / 'evolution/round-1/cards/0.json', {'old': 'card'})
    aligned.collect_cards(root, tasks, skills, aligned.METHOD)
    assert aligned.read(root / 'evolution/round-1/cards-before-task-skill-v1/0.json') == {'old': 'card'}
    before = {str(p): p.read_bytes() for p in root.rglob('*.json')}
    aligned.collect_cards(root, tasks, skills, aligned.METHOD)
    assert before == {str(p): p.read_bytes() for p in root.rglob('*.json')}
    loaded = driver(root, skills)._collect_evolution_cards(1)
    assert len(loaded) == 3


@pytest.mark.parametrize('failure', ['missing_reward', 'wrong_body', 'wrong_selection', 'wrong_model',
                                    'wrong_activation', 'wrong_task', 'wrong_trajectory', 'wrong_version'])
def test_invalid_original_evidence_blocks_grouping(sources, failure):
    root, tasks, skills = sources
    slot = root / 'slots/00-1'
    row = aligned.read(slot / 'record.json')
    result = Path(row['result_path'])
    value = aligned.read(result)
    if failure == 'missing_reward':
        value['verifier_result']['rewards']['reward'] = None
    elif failure == 'wrong_model':
        value['config']['agent']['model_name'] = 'openai/other'
    elif failure == 'wrong_task':
        value['task_name'] = 'task1'
    elif failure == 'wrong_body':
        Path(row['skill_file']).write_text('other skill')
    elif failure == 'wrong_selection':
        path = slot / 'requests/001/selection.json'
        selection = aligned.read(path)
        selection['loaded_skill_key'] = skills[1].key
        aligned.write(path, selection)
    elif failure == 'wrong_activation':
        path = result.parent / 'agent/skill_activation.json'
        activated = aligned.read(path)
        activated['load_stage'] = 'before_selection'
        aligned.write(path, activated)
    elif failure == 'wrong_trajectory':
        row['trajectory_path'] = str(root / 'unrelated.json')
        aligned.write(slot / 'record.json', row)
    else:
        row['selection']['loaded_skill_key'] = skills[0].skill_id + '@v1'
        aligned.write(slot / 'record.json', row)
    aligned.write(result, value)
    with pytest.raises(ValueError):
        aligned.grouped_cards(root, tasks, skills, aligned.METHOD)


def test_partial_coverage_stays_locked_without_publishing(sources):
    root, tasks, skills = sources
    path = root / 'slots/00-1/record.json'
    row = aligned.read(path)
    row.update(status='infrastructure_error', reward=None)
    aligned.write(path, row)
    cards, manifest, audit = aligned.grouped_cards(root, tasks, skills, aligned.METHOD)
    assert audit['valid_slots'] == 5 and audit['missing_slots'] == ['00-1'] and not audit['passed']
    with pytest.raises(ValueError, match='coverage'):
        aligned.collect_cards(root, tasks, skills, aligned.METHOD)
    assert not (root / 'execution_audit.json').exists()
    assert not (root / 'evolution').exists()


def test_manifest_cannot_omit_or_duplicate_source_attempt(sources):
    root, tasks, skills = sources
    cards, manifest, _ = aligned.grouped_cards(root, tasks, skills, aligned.METHOD)
    key = TS.experience_id(0, skills[0].key)
    trial = cards[key].l1_trials[1]
    trial.update(copy.deepcopy(cards[key].l1_trials[0]))
    trial['index'] = 2
    manifest['cards'][key] = S.content_hash(S.to_dict(cards[key]))
    with pytest.raises(ValueError, match='Duplicate'):
        TS.validate_cards(root, manifest, cards, skills, (0, 1))


def test_existing_l2_journal_blocks_migration(sources):
    root, tasks, skills = sources
    aligned.write(root / 'l2_batches/existing.json', {'history': True})
    with pytest.raises(ValueError, match='migrate'):
        aligned.collect_cards(root, tasks, skills, aligned.METHOD)
    assert not (root / 'execution_audit.json').exists()


def test_external_missing_manifest_never_starts_l1(sources, monkeypatch):
    root, _, skills = sources
    from skillexpand.runtime import parallel as PL
    monkeypatch.setattr(PL, 'run_generic', lambda *a, **k: pytest.fail('unexpected L1 execution'))
    with pytest.raises(FileNotFoundError):
        driver(root, skills)._collect_evolution_cards(1)


def test_external_missing_card_never_starts_l1(sources, monkeypatch):
    root, tasks, skills = sources
    aligned.collect_cards(root, tasks, skills, aligned.METHOD)
    next((root / 'evolution/round-1/cards').glob('*.json')).unlink()
    from skillexpand.runtime import parallel as PL
    monkeypatch.setattr(PL, 'run_generic', lambda *a, **k: pytest.fail('unexpected L1 execution'))
    with pytest.raises(FileNotFoundError):
        driver(root, skills)._collect_evolution_cards(1)


def test_cached_batch_restore_reads_grouped_card_files(sources, monkeypatch):
    root, tasks, skills = sources
    aligned.collect_cards(root, tasks, skills, aligned.METHOD)
    loop = driver(root, skills)
    loop.cards = loop._read_evolution_cards(1)
    batches = loop._evolution_batches(1, loop.cards)
    aligned.write(root / 'evolution/round-1/input.json',
                  {'skills': [S.to_dict(s) for s in skills], 'task_ids': [0, 1]})
    loop.skills.get = lambda key: next(s for s in skills if s.key == key)
    seen = []
    monkeypatch.setattr(A, 'audit_batch', lambda root, value, skill, cards:
                        seen.extend(c.experience_id for c in cards))
    for batch in batches:
        aligned.write(root / 'l2_batches' / (batch['batch_id'] + '.json'),
                      {**batch, 'base_skill_key': next(s.key for s in skills if s.family_id == batch['family_id']),
                       'candidate': None})
        loop._run_batch(batch)
    assert sorted(seen) == sorted(loop.cards)
    aligned.write(root / 'evolution/round-1/batches.json', list(reversed(batches)))
    restored = []
    loop._restore = lambda value: restored.append(value['batch_id'])
    loop._recover_transactions()
    assert restored == [b['batch_id'] for b in reversed(batches)]


def test_agent_timeout_with_real_reward_is_valid(sources):
    root, tasks, skills = sources
    row = aligned.read(root / 'slots/00-1/record.json')
    path = Path(row['result_path'])
    value = aligned.read(path)
    value['exception_info'] = {'exception_type': 'AgentTimeoutError'}
    aligned.write(path, value)
    _, _, audit = aligned.grouped_cards(root, tasks, skills, aligned.METHOD)
    assert audit['passed']


def test_missing_source_card_cannot_pass_complete_audit(sources):
    root, tasks, skills = sources
    cards, manifest, _ = aligned.grouped_cards(root, tasks, skills, aligned.METHOD)
    eid = TS.experience_id(0, skills[1].key)
    del cards[eid]
    del manifest['experiences'][eid]
    del manifest['cards'][eid]
    manifest['experience_ids'].remove(eid)
    with pytest.raises(ValueError, match='coverage'):
        TS.validate_cards(root, manifest, cards, skills, (0, 1))


def test_l2_only_blocks_before_reservation_or_relay(sources, monkeypatch):
    root, tasks, skills = sources
    aligned.write(root / 'alignment.json', {'executor_model': aligned.METHOD})
    aligned.write(root / 'status.json', {'status': 'needs_attention'})
    aligned.write(root / 'cold_start_complete.json', {'model_generated_library': True})
    row = aligned.read(root / 'slots/00-1/record.json')
    row['status'] = 'infrastructure_error'
    aligned.write(root / 'slots/00-1/record.json', row)
    monkeypatch.setattr(aligned, 'load_cold_start', lambda root: (None, None, skills, None))
    monkeypatch.setattr(aligned, 'reserve', lambda *a: pytest.fail('reserved before input audit'))
    monkeypatch.setattr(aligned, 'relay_from_env', lambda: pytest.fail('unexpected relay/model request'))
    with pytest.raises(ValueError, match='coverage'):
        aligned.execute(SimpleNamespace(l2_only=True), root)


def test_external_round_audit_uses_experience_ids_and_original_tasks(sources, monkeypatch):
    root, tasks, skills = sources
    aligned.collect_cards(root, tasks, skills, aligned.METHOD)
    cards = driver(root, skills)._read_evolution_cards(1)
    batches = driver(root, skills)._evolution_batches(1, cards)
    directory = root / 'evolution/round-1'
    aligned.write(directory / 'input.json', {'round': 1, 'task_ids': [0, 1],
                                          'skills': [S.to_dict(s) for s in skills]})
    aligned.write(root / 'split.json', {'benchmark': 'terminalbench', 'assignment': {'0': 'train', '1': 'train'}})
    aligned.write(root / 'config.json', {'benchmark': {'name': 'terminalbench', 'progressive_library': True}})
    aligned.write(root / 'l2_manifest.json', {'initial': [S.to_dict(s) for s in skills],
        'config': {'acceptance_mode': 'predicted', 'predicted_review_scope': 'val'}})
    (root / 'skills.jsonl').write_text(''.join(json.dumps(S.to_dict(s)) + '\n' for s in skills))
    aligned.write(directory / 'batches.json', batches)
    seen = []
    monkeypatch.setattr(A, 'resolve', lambda cfg: None)
    monkeypatch.setattr(A, 'audit_batch', lambda root, batch, base, evidence: seen.extend(e.experience_id for e in evidence))
    for batch in batches:
        skill = next(s for s in skills if s.family_id == batch['family_id'])
        aligned.write(root / 'l2_batches' / (batch['batch_id'] + '.json'), {**batch,
            'base_skill_key': skill.key, 'outcome': 'rejected', 'candidate': None,
            'acceptance_mode': 'predicted', 'predicted_review_scope': 'val',
            'empirically_validated': False,
            'acceptance': {'mode': 'predicted', 'executions': 0, 'scope': 'val', 'candidates': []}})
    result = A.audit_round(root, 1)
    assert result['tasks'] == 2 and result['experience_cards'] == 3
    assert sorted(seen) == sorted(cards)
    # A duplicate experience cannot pass the exactly-once batch audit.
    bad = copy.deepcopy(batches)
    bad[0]['experience_ids'].append(bad[0]['experience_ids'][0])
    aligned.write(directory / 'batches.json', bad)
    with pytest.raises(ValueError, match='frozen plan'):
        A.audit_round(root, 1)
