"""Raw input coverage requires real verifier outcomes, including valid zeroes."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('tb21_raw_gate',
    Path(__file__).resolve().parents[1] / 'scripts/validate_tb21_rollouts.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.mark.parametrize('exception,reason', [
    ({'exception_type': 'RuntimeError'}, 'runtime'),
    ({'exception_message': 'HTTP 503'}, 'provider_http'),
    ({'exception_message': 'HTTP 429'}, 'rate_limit'),
])
def test_exception_is_invalid_even_with_reward_zero(tmp_path, exception, reason):
    (tmp_path / 'agent').mkdir()
    (tmp_path / 'agent/trajectory.json').write_text(json.dumps({'messages': ['execution']}))
    result = {'exception_info': exception, 'verifier_result': {'rewards': {'reward': 0}}}
    assert gate._is_valid(result, tmp_path) == (False, reason)


@pytest.mark.parametrize('exception', [
    {'exception_type': 'AgentTimeoutError'},
    {'exception_type': 'RuntimeError', 'exception_message': 'operation timed out'},
])
def test_user_authorized_timeout_counts_as_valid_raw_rollout(tmp_path, exception):
    (tmp_path / 'agent').mkdir()
    (tmp_path / 'agent/trajectory.json').write_text('[{"execution": "timed out"}]')
    assert gate._is_valid({'exception_info': exception}, tmp_path) == (True, 'timeout_accepted')


@pytest.mark.parametrize('reward,valid', [(0, True), (1, True), (None, False),
                                        (True, False), (0.5, False)])
def test_only_binary_verifier_outcomes_are_valid(tmp_path, reward, valid):
    trial = tmp_path / 'fix-git__trial'
    (trial / 'agent').mkdir(parents=True)
    (trial / 'agent/trajectory.json').write_text(json.dumps({'messages': ['execution']}))
    (trial / 'result.json').write_text(json.dumps({'task_name': 'fix-git',
        'verifier_result': {'rewards': {'reward': reward}}}))
    report = gate.validate(tmp_path, {'fix-git'}, {'fix-git': 29})
    assert report['valid_rollout_count'] == int(valid)
    assert report['coverage'] == 'incomplete'
    assert report['rows'][0]['task_id'] == 29
    assert report['rows'][0]['attempt_index'] == 1


def test_missing_reward_and_missing_trajectory_are_invalid(tmp_path):
    assert gate._is_valid({}, tmp_path) == (False, 'missing_trajectory')
    (tmp_path / 'agent').mkdir()
    (tmp_path / 'agent/trajectory.json').write_text('[{}]')
    assert gate._is_valid({}, tmp_path) == (False, 'missing_reward')
