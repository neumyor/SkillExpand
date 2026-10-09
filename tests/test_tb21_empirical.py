"""Empirical scoring must never turn missing infrastructure results into zero."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('tb21_empirical_summary',
    Path(__file__).resolve().parents[1] / 'scripts/summarize_tb21_empirical.py')
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def test_complete_pass_at_one_and_three():
    rows = [{'task_id': t, 'attempt_index': a + 1, 'reward': reward}
            for t, rewards in enumerate(((0, 1, 0), (0, 0, 0)))
            for a, reward in enumerate(rewards)]
    result = summary.metrics(rows, 2)
    assert result['complete']
    assert result['pass_at_1'] == pytest.approx(1 / 6)
    assert result['pass_at_3'] == 0.5


def test_incomplete_panel_has_no_full_score_and_duplicates_are_rejected():
    rows = [{'task_id': 0, 'attempt_index': 1, 'reward': 1}]
    result = summary.metrics(rows, 2)
    assert not result['complete']
    assert result['pass_at_1'] is result['pass_at_3'] is None
    with pytest.raises(ValueError, match='duplicate'):
        summary.metrics(rows + rows, 2)


def trial(tmp_path, reward=0, exception=None):
    path = tmp_path / 'result.json'
    summary.write(path, {'task_name': 'fix-git', 'exception_info': exception,
        'verifier_result': {'rewards': {'reward': reward}},
        'config': {'agent': {'model_name': 'openai/qwen3.6-flash-distill',
                            'import_path': 'tencent_sandbox_terminus:TencentSkillTerminus2'}}})
    summary.write(tmp_path / 'agent/trajectory.json', {'messages': ['actual execution']})
    return path


def test_verifier_zero_is_valid_but_timeout_with_zero_is_excluded(tmp_path):
    path = trial(tmp_path)
    assert summary.validate_trial(path, 'fix-git')[1] == 0
    trial(tmp_path, exception={'exception_type': 'AgentTimeoutError'})
    with pytest.raises(ValueError, match='trial_exception:AgentTimeoutError'):
        summary.validate_trial(path, 'fix-git')


@pytest.mark.parametrize('reward', [None, True, -1, 0.4])
def test_invalid_rewards_are_excluded(tmp_path, reward):
    with pytest.raises(ValueError, match='invalid_verifier_reward'):
        summary.validate_trial(trial(tmp_path, reward), 'fix-git')


def test_skill_execution_requires_activation_and_exact_task_identity(tmp_path):
    path = trial(tmp_path, 1)
    with pytest.raises(OSError):
        summary.validate_trial(path, 'fix-git', activation=True)
    summary.write(tmp_path / 'agent/skill_activation.json', {'load_stage': 'after_selection'})
    assert summary.validate_trial(path, 'fix-git', activation=True)[1] == 1
    with pytest.raises(ValueError, match='task_identity_mismatch'):
        summary.validate_trial(path, 'other-task', activation=True)


def test_summary_checks_mounted_body_and_verifier_instead_of_trusting_record(tmp_path):
    skill = {'skill_id': 'repair', 'version': 1, 'description': 'repair git', 'body': 'Inspect then repair.'}
    summary.write(tmp_path / 'library.json', [skill])
    summary.write(tmp_path / 'manifest.json', {'tasks': 1, 'task_names': ['fix-git']})
    request = tmp_path / 'slots/00-1/requests/001'
    request.mkdir(parents=True)
    path = trial(request, 1)
    mounted = request / 'SKILL.md'
    mounted.write_text(skill['body'])
    summary.write(request / 'agent/skill_activation.json',
                  {'load_stage': 'after_selection', 'source_path': str(mounted)})
    record = {'status': 'valid', 'task_id': 0, 'attempt_index': 1, 'task_name': 'fix-git',
        'reward': 1, 'result_path': str(path), 'trajectory_path': str(request / 'agent/trajectory.json'),
        'skill_file': str(mounted), 'selection': {'ok': True, 'skill_id': 'repair',
            'loaded_skill_id': 'repair', 'loaded_skill_key': 'repair@v1',
            'load_stage': 'after_selection', 'catalog': [{'skill_id': 'repair', 'description': 'repair git'}]}}
    summary.write(tmp_path / 'slots/00-1/record.json', record)
    assert summary.summarize(tmp_path)['empirical']['valid_attempts'] == 1
    mounted.write_text('Different Skill')
    report = summary.summarize(tmp_path)
    assert report['empirical']['valid_attempts'] == 0
    assert report['rejected_slots'][0]['error'] == 'mounted_skill_body_mismatch'
    mounted.write_text(skill['body'])
    record['reward'] = 0
    summary.write(tmp_path / 'slots/00-1/record.json', record)
    assert summary.summarize(tmp_path)['rejected_slots'][0]['error'] == 'result_record_mismatch'
