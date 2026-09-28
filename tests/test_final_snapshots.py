import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('final_snapshots', Path(__file__).parents[1] / 'scripts/run_final_snapshots.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_historical_snapshots_use_round_end_keys_not_latest_heads():
    initial = [dict(skill_id='b.f', version=0, description='frozen', body='initial')]
    history = initial + [dict(skill_id='b.f', version=n, description='frozen', body=f'body{n}') for n in (1, 2, 3)]
    rounds = [dict(status='complete', skills={'b.f': 'b.f@v1'}), dict(status='complete', skills={'b.f': 'b.f@v3'})]
    result = runner.select_snapshots(initial, history, rounds)
    assert [result[label][0]['version'] for label in runner.LABELS] == [0, 1, 3]
    rounds[0]['skills']['b.f'] = 'b.f@v0'
    assert runner.select_snapshots(initial, history, rounds)['evolve-1'] == initial
    history[1]['description'] = 'changed routing'
    rounds[0]['skills']['b.f'] = 'b.f@v1'
    with pytest.raises(ValueError, match='descriptions changed'):
        runner.select_snapshots(initial, history, rounds)


def test_paired_results_include_routing_failures_and_require_identical_population(tmp_path):
    results = {}
    for label, outcomes in zip(runner.LABELS, ([True, False], [False, True], [False, False])):
        target = tmp_path / label
        target.mkdir()
        (target / 'scores.jsonl').write_text('\n'.join(json.dumps(dict(task_id=t, success=v)) for t, v in enumerate(outcomes)))
        results[label] = dict(target=str(target), tasks=3, routing_failures=[2])
    paired = runner.paired_results(results)
    assert paired['cold-start->evolve-1']['correct_to_wrong'] == dict(count=1, task_ids=[0])
    assert paired['cold-start->evolve-1']['wrong_to_correct'] == dict(count=1, task_ids=[1])
    assert paired['cold-start->evolve-1']['both_wrong'] == dict(count=1, task_ids=[2])
    results['evolve-2']['routing_failures'] = [3]
    with pytest.raises(ValueError, match='populations differ'):
        runner.paired_results(results)


def test_usage_audit_rejects_duplicate_requests_and_wrong_totals(tmp_path):
    ledger = tmp_path / 'a.requests.jsonl'
    rows = [dict(event='start', run_id='request1'), dict(event='end', run_id='request1',
        provider=dict(token_usage=dict(prompt_tokens=10, completion_tokens=2, total_tokens=12)))]
    ledger.write_text('\n'.join(map(json.dumps, rows)))
    runner.save(tmp_path / 'a.json', dict(started_requests=1, successful_requests=1, failed_requests=0,
        prompt_tokens=10, completion_tokens=2, total_tokens=12))
    assert runner.audit_token_ledgers(tmp_path)['total_tokens'] == 12
    ledger.write_text('\n'.join(map(json.dumps, rows + rows)))
    with pytest.raises(ValueError, match='Duplicate'):
        runner.audit_token_ledgers(tmp_path)
    ledger.write_text('\n'.join(map(json.dumps, rows)))
    totals = runner.read(tmp_path / 'a.json')
    totals['total_tokens'] = 99
    runner.save(tmp_path / 'a.json', totals)
    with pytest.raises(ValueError, match='totals disagree'):
        runner.audit_token_ledgers(tmp_path)


def test_frozen_files_are_verified_before_resume(tmp_path):
    frozen = tmp_path / 'input.json'
    frozen.write_text('{}')
    runner.save(tmp_path / 'manifest.json', dict(files={'input.json': runner.digest(frozen)}))
    runner.verify(tmp_path)
    frozen.write_text('{"modified":true}')
    with pytest.raises(ValueError, match='Frozen input/code changed'):
        runner.verify(tmp_path)


def test_task_identity_ignores_only_relocation_and_keeps_environment_controls():
    from omegaconf import OmegaConf
    def table(path, steps):
        return [dict(task='goal', env_kwargs=dict(gamefile='game', config=OmegaConf.create(dict(task_file=path, max_steps=steps))))]
    assert runner.task_identity(table('old.json', 20)) == runner.task_identity(table('new.json', 20))
    assert runner.task_identity(table('old.json', 20)) != runner.task_identity(table('new.json', 21))
