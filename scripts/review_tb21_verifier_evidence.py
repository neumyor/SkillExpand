"""Write a supplementary verifier review without rewriting panel history."""
import argparse
from pathlib import Path

from summarize_tb21_empirical import read, validate_trial, write


def review(root):
    manifest = read(root / 'manifest.json')
    valid, rejected = [], []
    for path in sorted((root / 'slots').glob('*/record.json')):
        row = read(path)
        if row.get('status') != 'valid':
            continue
        try:
            _, reward, _ = validate_trial(row['result_path'], row['task_name'],
                activation=True, model=manifest.get('model', 'qwen3.6-flash-distill'))
            valid.append({'slot': path.parent.name, 'reward': reward, 'result': row['result_path']})
        except (ValueError, OSError) as exc:
            rejected.append({'slot': path.parent.name, 'task': row['task_name'],
                'observed_reward': row['reward'], 'accepted_reward': None,
                'result': row['result_path'], 'reason': str(exc)})
    report = {'complete': len(valid) == 267 and not rejected,
              'valid_scores': len(valid), 'unique_results': len({r['result'] for r in valid}),
              'rejected': rejected, 'valid': valid,
              'policy': 'Require matching verifier reward file and executed pytest outcomes; '
                        'missing outcomes, including crashes, remain unresolved for review.',
              'historical_records_unchanged': True}
    write(root / 'verifier_evidence_audit.json', report)
    print(root, report['valid_scores'], 'rejected', rejected)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', action='append', type=Path, required=True)
    for directory in parser.parse_args().run_dir:
        review(directory)
