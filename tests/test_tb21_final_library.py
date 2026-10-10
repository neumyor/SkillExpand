import os
from pathlib import Path

import pytest

import run_tb21_empirical as empirical


@pytest.mark.parametrize('stage,source_stage,executor,selector', [
    ('E3_FINAL', 'E3', 'DEEPSEEK_up5zdj', 'DEEPSEEK_up5zdj'),
    ('E4', 'E3', 'qwen3.6-flash-distill', 'DEEPSEEK_up5zdj'),
    ('E5_FINAL', 'E5', 'qwen3.6-flash-distill', 'qwen3.6-flash-distill'),
    ('E6_FINAL', 'E6', 'qwen3.6-flash-distill', 'qwen3.6-flash-distill'),
])
def test_final_snapshot_preserves_all_versions_and_explicit_roles(tmp_path, monkeypatch,
                                                                stage, source_stage, executor, selector):
    source = tmp_path / 'source'
    empirical.write(source / 'alignment.json', {'stage': source_stage, 'method_model': 'DEEPSEEK_up5zdj'})
    empirical.write(source / 'config.json', {'agent': {'llm': 'old'}, 'models': {'selector': 'old'}})
    empirical.write(source / 'tasks.json', [{'task_name': str(i), 'instruction': str(i)} for i in range(89)])
    empirical.write(source / 'evolution/round-1/audit.json', {'round': 1})
    heads = [{'skill_id': 'a', 'version': 1, 'body': 'v1'}, {'skill_id': 'b', 'version': 0, 'body': 'v0'}]
    monkeypatch.setattr(empirical, 'final_heads', lambda path: heads)
    monkeypatch.setattr(empirical, 'active_worker_reservation', lambda *args: (0, []))
    root = tmp_path / 'final'
    empirical.prepare(source, root, 16, stage)
    manifest = empirical.read(root / 'manifest.json')
    assert (manifest['model'], manifest['selector_model']) == (executor, selector)
    assert empirical.read(root / 'library.json') == heads
    assert manifest['library_versions'] == {'a': 'a@v1', 'b': 'b@v0'}
    assert not list(root.glob('slots/*'))


def test_incomplete_source_cannot_freeze(tmp_path):
    empirical.write(tmp_path / 'status.json', {'status': 'running'})
    empirical.write(tmp_path / 'result.json', {'status': 'partial'})
    with pytest.raises(ValueError, match='completed aligned L2'):
        empirical.final_heads(tmp_path)


def test_invalid_source_batches_cannot_freeze(tmp_path):
    empirical.write(tmp_path / 'status.json', {'status': 'complete'})
    empirical.write(tmp_path / 'result.json', {'status': 'complete', 'invalid_batches': 1})
    with pytest.raises(ValueError, match='invalid batches'):
        empirical.final_heads(tmp_path)


def test_e4_uses_independent_selector_and_executor_credentials(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'method-test')
    monkeypatch.setenv('TB21_EXECUTOR_API_KEY', 'executor-test')
    monkeypatch.delenv('TB21_METHOD_API_KEY', raising=False)
    key = empirical.configure_role_credentials({'model': 'qwen3.6-flash-distill',
                                                'selector_model': 'DEEPSEEK_up5zdj'})
    assert key == 'executor-test'
    assert os.environ['OPENAI_API_KEY'] == 'method-test'


def test_e5_final_uses_qwen_for_both_roles(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'method-test')
    monkeypatch.setenv('TB21_EXECUTOR_API_KEY', 'executor-test')
    key = empirical.configure_role_credentials({'model': 'qwen3.6-flash-distill',
                                                'selector_model': 'qwen3.6-flash-distill'})
    assert key == os.environ['OPENAI_API_KEY'] == 'executor-test'


def test_final_evaluation_rejects_unfinished_request(tmp_path, monkeypatch):
    empirical.write(tmp_path / 'manifest.json', {'source_kind': 'aligned_evolved_final_library'})
    (tmp_path / 'slots/00-1/requests/001').mkdir(parents=True)
    monkeypatch.setattr(empirical, 'reconcile_slot', lambda *args: None)
    with pytest.raises(ValueError, match='Unfinished request'):
        empirical.execute_slot(None, None, None, tmp_path, None, 0, 1)


def test_final_reconciliation_keeps_earliest_real_reward(tmp_path):
    skill = {'skill_id': 'a', 'version': 1, 'description': 'skill', 'body': 'frozen body'}
    empirical.write(tmp_path / 'manifest.json', {'model': 'DEEPSEEK_up5zdj', 'task_names': ['task']})
    empirical.write(tmp_path / 'library.json', [skill])
    selection = {'ok': True, 'skill_id': 'a', 'loaded_skill_id': 'a', 'loaded_skill_key': 'a@v1',
                 'load_stage': 'after_selection', 'catalog': [{'skill_id': 'a', 'description': 'skill'}]}
    for index, reward in [(1, 0), (2, 1)]:
        request = tmp_path / f'slots/00-1/requests/{index:03d}'
        path = request / 'jobs/job/trial/result.json'
        empirical.write(path, {'task_name': 'task', 'verifier_result': {'rewards': {'reward': reward}},
            'config': {'agent': {'model_name': 'openai/DEEPSEEK_up5zdj',
                                'import_path': 'adapter:TencentSkillTerminus2'}}})
        mounted = request / 'SKILL.md'
        mounted.write_text(skill['body'])
        empirical.write(request / 'selection.json', selection)
        empirical.write(path.parent / 'agent/trajectory.json', {'steps': ['real']})
        empirical.write(path.parent / 'agent/skill_activation.json',
                        {'source_path': str(mounted), 'load_stage': 'after_selection'})
    first = empirical.reconcile_slot(tmp_path, 0, 1)
    assert first['reward'] == 0 and '/001/' in first['result_path']
    assert empirical.reconcile_slot(tmp_path, 0, 1) == first
    Path(first['skill_file']).write_text('wrong body')
    second = empirical.reconcile_slot(tmp_path, 0, 1)
    assert second['reward'] == 1 and '/002/' in second['result_path']
