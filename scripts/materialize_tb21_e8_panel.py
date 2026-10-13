"""Copy the audited E8 selections into a standalone 267-trial panel."""
import argparse
from collections import Counter
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--registration', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads((args.registration / 'execution_audit.json').read_text())
    rows = audit['rows']
    counts = Counter(row['task'] for row in rows)
    if (audit.get('complete') is not True or len(rows) != 267 or len(counts) != 89
            or set(counts.values()) != {3} or len({r['trial'] for r in rows}) != 267
            or len({r['result'] for r in rows}) != 267
            or any(r['reward'] not in (0, 1) or r['issues'] for r in rows)):
        raise ValueError('Expected an accepted unique 89 x 3 panel')
    if args.out.exists():
        raise ValueError('Output already exists; refusing to replace it')
    staging = args.out.with_name(args.out.name + '.staging')
    staging.mkdir(exist_ok=True)
    repo = args.registration.resolve().parents[1]
    mapping = []
    for row in sorted(rows, key=lambda r: (r['task'], r['trial'])):
        source = Path(row['result']).parent
        if not source.is_absolute():
            source = repo / source
        trajectory = source / 'agent/trajectory.json'
        if not json.loads(trajectory.read_text()):
            raise ValueError(f'Empty trajectory: {trajectory}')
        result = json.loads((source / 'result.json').read_text())
        if (result['trial_name'] != row['trial'] or result['task_name'] != row['task']
                or result['verifier_result']['rewards']['reward'] != row['reward']
                or float((source / 'verifier/reward.txt').read_text()) != row['reward']):
            raise ValueError(f'Source identity/reward differs: {source}')
        shutil.copytree(source, staging / row['trial'], dirs_exist_ok=True)
        mapping.append({**row, 'source_result': str(source / 'result.json'),
                        'result': str(args.out / row['trial'] / 'result.json'),
                        'trajectory': str(args.out / row['trial'] / 'agent/trajectory.json')})
    for name in ('report.json', 'full_config.json', 'execution_audit.json'):
        shutil.copy2(args.registration / name, staging / name)
    manifest = {'experiment': 'E8', 'complete': True, 'trials': 267, 'tasks': 89,
                'attempts_per_task': 3, 'source_audit': str(args.registration / 'execution_audit.json'),
                'historical_paths_in_trial_artifacts_preserved': True, 'rows': mapping}
    (staging / 'panel_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (staging / 'README.md').write_text(
        '# E8 Accepted 267-Trial Panel\n\n'
        '89 tasks, three audited trials each. Each trial contains agent/trajectory.json,\n'
        'result.json and verifier evidence copied from the earliest accepted execution.\n'
        'panel_manifest.json maps each copy to its original execution. Historical paths\n'
        'inside copied artifacts are preserved. This panel must not be resumed as a job.\n')
    if len(list(staging.glob('*/agent/trajectory.json'))) != 267:
        raise ValueError('Copied trajectory coverage differs')
    staging.rename(args.out)
    print(json.dumps({'complete': True, 'trials': 267, 'out': str(args.out)}))


if __name__ == '__main__':
    main()
