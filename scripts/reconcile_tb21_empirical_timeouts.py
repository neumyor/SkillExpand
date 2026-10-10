"""Choose the earliest verifier-valid request per slot, including agent timeouts."""
import argparse
from pathlib import Path

from summarize_tb21_empirical import read, write, validate_trial, summarize


def reconcile(root, *, empirical_report=True):
    root = root.resolve()
    manifest = read(root / 'manifest.json')
    task_names = manifest.get('task_names') or [t['task_name'] for t in read(root / 'tasks.json')]
    model = manifest.get('executor_model', 'qwen3.6-flash-distill')
    selected, rejected = [], []
    for slot in sorted((root / 'slots').iterdir()):
        if not slot.is_dir():
            continue
        task, attempt = map(int, slot.name.split('-'))
        for request in sorted((slot / 'requests').glob('*')):
            candidates = list(request.glob('jobs/*/*/result.json'))
            if len(candidates) != 1:
                continue
            path = candidates[0]
            try:
                result, reward, trajectory = validate_trial(path, task_names[task], activation=True, model=model)
                selection = read(request / 'selection.json')
                activation = read(path.parent / 'agent/skill_activation.json')
                row = {'task_id': task, 'attempt_index': attempt,
                       'task_name': task_names[task], 'status': 'valid',
                       'reward': reward, 'selection': selection,
                       'skill_file': activation['source_path'], 'result_path': str(path),
                       'trajectory_path': trajectory,
                       'exception_info': result.get('exception_info'),
                       'selection_policy': 'earliest_request_with_valid_verifier'}
                existing = slot / 'record.json'
                old = read(existing) if existing.exists() else None
                if old != row:
                    archive = slot / 'record_before_timeout_reconciliation.json'
                    if old and not archive.exists():
                        write(archive, old)
                    write(existing, row)
                selected.append({'slot': slot.name, 'request': request.name,
                                 'reward': reward, 'exception': result.get('exception_info', {}).get('exception_type')
                                 if result.get('exception_info') else None})
                break
            except (ValueError, KeyError, OSError) as exc:
                rejected.append({'slot': slot.name, 'request': request.name, 'reason': str(exc)})
    write(root / 'timeout_reconciliation.json', {
        'policy': 'AgentTimeoutError accepted only with real valid verifier reward; earliest valid request wins',
        'selected': selected, 'rejected_requests': rejected, 'rewards_fabricated': False})
    return summarize(root) if empirical_report else {'selected_slots': len(selected)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    report = reconcile(args.run_dir)
    print({k: v for k, v in report['empirical'].items() if k != 'per_task'})
