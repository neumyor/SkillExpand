"""Recheck the accepted E7 revision without dispatching benchmark requests."""
from collections import Counter
from pathlib import Path

import run_tb21_aligned as aligned
from skillexpand.l2.loop import RunLock
from skillexpand.persistence.artifacts import load_cold_start


ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/tb21-qwen-deepseek-library-qwen-selector-20261011/revisions/178-cards-v1')


def refresh(root=ROOT):
    with RunLock(root / 'driver.pid'):
        rejected = []
        for path in sorted((root / 'slots').glob('*/record.json')):
            row = aligned.read(path)
            if row.get('status') != 'valid':
                continue
            try:
                aligned.validate_trial(row['result_path'], row['task_name'],
                                       activation=True, model=aligned.QWEN)
            except ValueError as exc:
                if str(exc) != 'verifier_build_dependency_unavailable':
                    raise
                backup = path.parent / 'record.before-build-dependency-rejection.json'
                if not backup.exists():
                    aligned.write(backup, row)
                rejected.append({'slot': path.parent.name, 'observed_reward': row['reward'],
                                 'accepted_reward': None, 'result_path': row['result_path'],
                                 'reason': str(exc)})
                aligned.write(path, {**row, 'status': 'infrastructure_error',
                                     'observed_reward': row['reward'], 'reward': None,
                                     'error_class': 'verifier_build_dependency_unavailable',
                                     'error': str(exc)})
        _, _, skills, _ = load_cold_start(root)
        tasks = aligned.read(root / 'tasks.json')
        _, _, gate = aligned.grouped_cards(root, tasks, skills, aligned.QWEN)
        valid, missing = [], []
        for path in sorted((root / 'slots').glob('*/record.json')):
            row = aligned.read(path)
            if row.get('status') == 'valid':
                valid.append({'slot': path.parent.name, 'reward': row['reward'],
                              'result_path': row['result_path']})
            else:
                missing.append({'slot': path.parent.name, 'error': row.get('error')})
        counts = Counter(row['reward'] for row in valid)
        report = {'valid_slots': len(valid), 'expected_slots': 267,
                  'successes': counts[1], 'failures': counts[0], 'rejected': missing,
                  'complete': gate['passed'], 'source_gate': gate,
                  'L2_unlocked': False, 'new_requests': 0,
                  'raw_results_unchanged': True, 'valid': valid}
        previous = root / 'verifier_recovery_rejections_20261013.json'
        prior = aligned.read(previous) if previous.exists() else []
        aligned.write(previous, prior + rejected)
        aligned.write(root / 'verifier_recovery_gate_20261013.json', gate)
        aligned.write(root / 'verifier_recovery_progress_20261013.json', report)
        aligned.write(root / 'status.json', {'stage': 'QD',
            'status': 'initial_panel_complete' if gate['passed'] else 'needs_attention',
            'phase': 'awaiting_l2' if gate['passed'] else 'initial_audit',
            'workers': 0, 'coverage': f'{len(valid)}/267',
            'error': 'Incomplete valid slot coverage; L2 remains locked' if missing else None})
        print({key: report[key] for key in ('valid_slots', 'successes', 'failures', 'rejected')})
    return report


if __name__ == '__main__':
    refresh()
