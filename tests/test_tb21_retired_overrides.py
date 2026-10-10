from pathlib import Path

import apply_tb21_task_timeout_zero as scoring


def setup_slot(root):
    scoring.write(root / 'manifest.json', {'executor_model': 'model', 'task_names': [scoring.TASK]})
    slot = root / 'slots/00-1'
    scoring.write(slot / 'record.json', {'task_id': 0, 'attempt_index': 1,
                  'task_name': scoring.TASK, 'status': 'infrastructure_error'})
    scoring.write(root / 'task_timeout_score_overrides.json', {
        'policy': scoring.POLICY, 'task_name': scoring.TASK,
        'overrides': {}, 'retired_overrides': {'00-1': {'reward': 0}},
    })
    return slot


def test_retired_timeout_never_recreates_fixed_zero(tmp_path, monkeypatch):
    slot = setup_slot(tmp_path)
    result = slot / 'requests/001/jobs/job/trial/result.json'
    scoring.write(result, {'exception_info': {'exception_type': 'VerifierTimeoutError'}})
    def reject(*args, **kwargs):
        raise ValueError('trial_exception:VerifierTimeoutError')
    monkeypatch.setattr(scoring, 'validate_trial', reject)
    for _ in range(2):
        report = scoring.apply(tmp_path)
        assert report['panel']['valid_attempts'] == 0
        assert report['timeout_zero_slots'] == []
        assert len(report['unresolved_slots']) == 1
    assert scoring.read(tmp_path / 'task_timeout_score_overrides.json')['overrides'] == {}


def test_retired_slot_uses_earliest_real_score_and_preserves_history(tmp_path, monkeypatch):
    slot = setup_slot(tmp_path)
    skill = {'skill_id': 'p003', 'version': 0, 'description': 'pipeline', 'body': 'actual skill'}
    scoring.write(tmp_path / 'initial_skills.json', [skill])
    selection = {'ok': True, 'catalog': [{'skill_id': 'p003', 'description': 'pipeline'}],
                 'skill_id': 'p003', 'loaded_skill_id': 'p003', 'loaded_skill_key': 'p003@v0',
                 'load_stage': 'after_selection'}
    for index, reward in [(1, 0), (2, 1)]:
        request = slot / f'requests/{index:03d}'
        result = request / 'jobs/job/trial/result.json'
        scoring.write(result, {'reward': reward})
        mounted = request / 'skills/SKILL.md'
        mounted.parent.mkdir(parents=True)
        mounted.write_text(skill['body'])
        scoring.write(request / 'selection.json', selection)
        scoring.write(result.parent / 'agent/skill_activation.json', {'source_path': str(mounted)})
    def validate(path, *args, **kwargs):
        path = Path(path)
        result = scoring.read(path)
        return result, result['reward'], str(path.parent / 'agent/trajectory.json')
    monkeypatch.setattr(scoring, 'validate_trial', validate)
    scoring.apply(tmp_path)
    accepted = scoring.read(slot / 'record.json')
    assert accepted['reward'] == 0
    assert '/001/' in accepted['result_path']
    assert (slot / 'record_before_pipeline_verifier_rerun.json').exists()
    scoring.apply(tmp_path)
    assert scoring.read(slot / 'record.json') == accepted
    assert scoring.read(tmp_path / 'task_timeout_score_overrides.json')['overrides'] == {}
