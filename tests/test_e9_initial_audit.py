import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audit_tb21_e9_initial import audit
from summarize_tb21_empirical import write


def test_initial_audit_preserves_framework_zero_and_absent_task_as_missing(tmp_path):
    write(tmp_path / 'run/contract.json', {'brew_tasks': ['success', 'unverified', 'absent']})
    for index, (task, reward) in enumerate([('success', 1), ('unverified', None)]):
        trial = tmp_path / f'trial-{index}'
        write(trial / 'result.json', {'task_name': task,
              'config': {'agent': {'model_name': 'openai/qwen3.6-flash-distill'}},
              'verifier_result': {'rewards': {'reward': reward}}})
        write(trial / 'agent/trajectory.json', {'steps': [{'message': 'executed'}]})
        (trial / 'verifier').mkdir()
        (trial / 'verifier/reward.txt').write_text(str(reward))
        (trial / 'verifier/test-stdout.txt').write_text('test session starts\n1 passed in 1s')
        write(tmp_path / f'run/brew/{index:03d}-{task}/state.json',
              {'status': 'completed', 'initial': {'task': task, 'reward': reward or 0,
               'raw_result_path': str(trial / 'result.json')}})
    report = audit(tmp_path)
    assert (report['reward1'], report['reward0'], report['missing']) == (1, 0, 2)
    assert report['rows'][1]['reported_reward'] == 0
    assert report['rows'][1]['reward'] is None
    assert report['rows'][2]['status'] == 'missing_state'
    assert not report['complete']
