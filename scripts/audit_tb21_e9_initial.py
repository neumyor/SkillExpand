"""Audit E9 initial outcomes using raw verifier evidence, including absent states."""
import argparse
from pathlib import Path

from summarize_tb21_empirical import read, validate_trial, write


def audit(output):
    run = output / 'run'
    expected = read(run / 'contract.json')['brew_tasks']
    states = {}
    for path in sorted((run / 'brew').glob('*/state.json')):
        state = read(path)
        task = (state.get('initial') or {}).get('task') or path.parent.name.split('-', 1)[1]
        if task in states:
            raise ValueError('duplicate task state: ' + task)
        states[task] = state
    rows, seen = [], set()
    for task in expected:
        state = states.get(task, {})
        trial = state.get('initial') or {}
        row = {'task': task, 'status': state.get('status', 'missing_state'),
               'reported_reward': trial.get('reward'), 'reward': None,
               'raw_result_path': trial.get('raw_result_path')}
        try:
            if not row['raw_result_path']:
                raise ValueError('missing_raw_result')
            result = Path(row['raw_result_path']).resolve()
            if result in seen:
                raise ValueError('duplicate_raw_result')
            _, reward, _ = validate_trial(result, task, activation=False,
                                         model='qwen3.6-flash-distill')
            seen.add(result)
            row['reward'] = reward
        except (ValueError, OSError) as exc:
            row['reason'] = str(exc)
        rows.append(row)
    return {'complete': len(rows) == 89 and all(r['reward'] is not None for r in rows),
            'expected_tasks': len(expected), 'task_states': len(states),
            'reward1': sum(r['reward'] == 1 for r in rows),
            'reward0': sum(r['reward'] == 0 for r in rows),
            'missing': sum(r['reward'] is None for r in rows), 'rows': rows,
            'policy': 'Raw verifier reward, executed test outcomes, model, identity and trajectory required; framework zeros are not scores.',
            'closed_set': True, 'historical_states_unchanged': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--registration', type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.output)
    prior = args.registration / 'initial_audit.json'
    backup = args.registration / 'initial_audit.before-raw-verifier-review.json'
    if prior.exists() and not backup.exists():
        write(backup, read(prior))
    write(prior, report)
    print({key: value for key, value in report.items() if key != 'rows'})
