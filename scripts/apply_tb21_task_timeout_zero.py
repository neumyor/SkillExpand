"""Apply an explicit task timeout scoring policy without changing raw trials."""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from summarize_tb21_empirical import metrics, read, validate_trial, write


TASK = 'torch-pipeline-parallelism'
POLICY = 'user_requested_torch_pipeline_timeout_zero'


def apply(root, *, retire_overrides=False):
    root = Path(root).resolve()
    manifest = read(root / 'manifest.json')
    model = manifest['executor_model']
    task_names = manifest.get('task_names') or [t['task_name'] for t in read(root / 'tasks.json')]
    ledger_path = root / 'task_timeout_score_overrides.json'
    ledger = read(ledger_path) if ledger_path.exists() else {
        'policy': POLICY, 'task_name': TASK,
        'authorization': 'User: torch-pipeline-parallelism 的超时视为0',
        'raw_verifier_results_modified': False, 'overrides': {},
    }
    if ledger['policy'] != POLICY or ledger['task_name'] != TASK:
        raise ValueError('Existing scoring policy does not match')
    retired = ledger.setdefault('retired_overrides', {})
    if retire_overrides:
        for slot, entry in list(ledger['overrides'].items()):
            retired[slot] = {**entry, 'retired_at': datetime.now(timezone.utc).isoformat(),
                             'retirement_reason': 'User requested real verifier rerun score instead of fixed zero'}
            del ledger['overrides'][slot]
        write(ledger_path, ledger)
    rows, unresolved = [], []
    for slot in sorted((root / 'slots').iterdir()):
        if not slot.is_dir() or not (slot / 'record.json').exists():
            continue
        row = read(slot / 'record.json')
        if slot.name in retired:
            if (slot.name != f"{row['task_id']:02d}-{row['attempt_index']}"
                    or row['task_name'] != TASK or task_names[row['task_id']] != TASK):
                raise ValueError('Retired slot identity mismatch')
            for request in sorted((slot / 'requests').glob('*')):
                candidates = list(request.glob('jobs/*/*/result.json'))
                if len(candidates) != 1:
                    continue
                try:
                    result, reward, trajectory = validate_trial(candidates[0], TASK, activation=True, model=model)
                    selection = read(request / 'selection.json')
                    activation = read(candidates[0].parent / 'agent/skill_activation.json')
                    library = root / 'initial_skills.json'
                    skills = read(library if library.exists() else root / 'library.json')
                    skill = next(s for s in skills if s['skill_id'] == selection['loaded_skill_id'])
                    catalog = [{'skill_id': s['skill_id'], 'description': s['description']}
                               for s in sorted(skills, key=lambda s: s['skill_id'])]
                    if (not selection['ok'] or selection['catalog'] != catalog
                            or selection['skill_id'] != skill['skill_id']
                            or selection['loaded_skill_key'] != f"{skill['skill_id']}@v{skill['version']}"
                            or selection['load_stage'] != 'after_selection'):
                        raise ValueError('Selected skill identity mismatch')
                    mounted = Path(activation['source_path'])
                    if (not mounted.resolve().is_relative_to(request.resolve())
                            or mounted.read_text() != skill['body']):
                        raise ValueError('Mounted skill body mismatch')
                except (ValueError, KeyError, OSError, StopIteration):
                    continue
                replacement = {'task_id': row['task_id'], 'attempt_index': row['attempt_index'],
                    'task_name': TASK, 'status': 'valid', 'reward': reward,
                    'score_source': 'real_verifier', 'selection_policy': 'earliest_request_with_valid_verifier',
                    'selection': selection, 'skill_file': str(mounted), 'result_path': str(candidates[0]),
                    'trajectory_path': trajectory, 'exception_info': result.get('exception_info')}
                if row != replacement:
                    archive = slot / 'record_before_pipeline_verifier_rerun.json'
                    if not archive.exists():
                        write(archive, row)
                    write(slot / 'record.json', replacement)
                row = replacement
                break
        if (slot.name not in ledger['overrides'] and slot.name not in retired
                and row['task_name'] == TASK and row['status'] != 'valid'):
            for request in sorted((slot / 'requests').glob('*')):
                candidates = list(request.glob('jobs/*/*/result.json'))
                if len(candidates) != 1:
                    continue
                path = candidates[0]
                result = read(path)
                exception = result.get('exception_info') or {}
                if exception.get('exception_type') not in ('AgentTimeoutError', 'VerifierTimeoutError'):
                    continue
                if result.get('task_name') != TASK or not result.get('finished_at'):
                    continue
                agent = result.get('config', {}).get('agent', {})
                if agent.get('model_name') != 'openai/' + model.removeprefix('openai/'):
                    raise ValueError('Timeout executor model mismatch')
                trajectory = path.parent / 'agent/trajectory.json'
                activation = path.parent / 'agent/skill_activation.json'
                if not trajectory.exists() or not read(trajectory) or not activation.exists():
                    continue
                if read(activation).get('load_stage') != 'after_selection':
                    continue
                selection = read(request / 'selection.json')
                ledger['overrides'][slot.name] = {
                    'task_id': row['task_id'], 'attempt_index': row['attempt_index'],
                    'task_name': TASK, 'reward': 0, 'score_source': POLICY,
                    'request': request.name, 'result_path': str(path),
                    'trajectory_path': str(trajectory), 'selection': selection,
                    'exception_info': exception,
                    'raw_verifier_result': result.get('verifier_result'),
                    'record_status_at_policy_application': row['status'],
                    'applied_at': datetime.now(timezone.utc).isoformat(),
                }
                break
        override = ledger['overrides'].get(slot.name)
        if override:
            rows.append(override)
        elif row['status'] == 'valid':
            _, reward, _ = validate_trial(row['result_path'], row['task_name'], activation=True, model=model)
            if reward != row['reward']:
                raise ValueError('Record reward mismatch')
            rows.append(row)
        else:
            unresolved.append({'slot': slot.name, 'task_name': row['task_name'], 'error': row.get('error')})
    write(ledger_path, ledger)
    panel = metrics(rows, len(task_names))
    report = {
        'policy': POLICY, 'scope': 'closed-set; verifier scores plus explicit user timeout-zero overrides',
        'panel': panel, 'timeout_zero_slots': sorted(ledger['overrides']),
        'retired_timeout_zero_slots': sorted(retired),
        'verifier_valid_slots': sum(read(p).get('status') == 'valid' for p in root.glob('slots/*/record.json')),
        'unresolved_slots': unresolved, 'execution_audit_changed': False,
    }
    write(root / 'policy_scoring.json', report)
    print(root.name, {'scored_slots': panel['valid_attempts'], 'timeout_zero_slots': report['timeout_zero_slots'],
                      'remaining': unresolved})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--retire-overrides', action='store_true')
    parser.add_argument('run_dirs', type=Path, nargs='+')
    args = parser.parse_args()
    for directory in args.run_dirs:
        apply(directory, retire_overrides=args.retire_overrides)
