"""Record strict E9 progress including separately selected recovery evidence."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from hkuds_e9_recovery import BASE, REPO, relocated


def main():
    sys.path[:0] = [str(BASE / 'src'), str(REPO / 'src')]
    from tbench_eval.agentbrew.processes import process_matches
    from tbench_eval.agentbrew.storage import read, save
    from run_hkuds_upskill_tb21 import true_reward
    out = BASE / 'outputs/E9_hkuds_upskill_acp_evo89_test89_20261013'
    root = out / 'run'
    original, selected = {}, {}
    for path in sorted((root / 'baseline').glob('*/rollout.json')):
        original[path.parent.name] = read(path)
        value = original[path.parent.name]
        selection = path.parent / 'recovery_selection_20261015.json'
        if selection.exists():
            sidecar = read(selection)
            if sidecar.get('authorization') != 'user_approved_plan_20261015':
                raise ValueError('Unknown baseline selection authorization')
            value = read(relocated(sidecar['selected_rollout']))
            if true_reward(value) is None:
                raise ValueError('Selected recovery evidence has no real reward')
        selected[path.parent.name] = value
    valid = [value for value in selected.values() if true_reward(value) is not None]
    teacher_requests = []
    for path in sorted(root.glob('**/http-*.json')):
        record = read(path)
        teacher_requests.append({'path': str(path.relative_to(root)),
            'status': record['status'], 'usage': record.get('usage'),
            'error_type': record.get('error_type'), 'outcome_unknown': record.get('outcome_unknown')})
    processes = {}
    for name in ['supervisor_identity.json', 'child.json']:
        identity = read(out / name)
        processes[name] = {'pid': identity['pid'], 'strong_identity_alive': process_matches(identity)}
    checkpoint = {'checked_at': datetime.now(timezone.utc).isoformat(),
        'status': read(root / 'status.json'), 'processes': processes,
        'baseline': {'units': len(selected), 'valid': len(valid),
            'success': sum(true_reward(value) == 1 for value in valid),
            'failure': sum(true_reward(value) == 0 for value in valid),
            'missing': [task for task, value in selected.items() if true_reward(value) is None],
            'original_valid': sum(true_reward(value) is not None for value in original.values())},
        'brew_complete_acps': len(list((root / 'brew').glob('*/acp.json'))),
        'teacher_request_status': dict(Counter(row['status'] for row in teacher_requests)),
        'teacher_requests': teacher_requests, 'teacher_turn_limit': 8, 'budget_resets': 0,
        'amendment': read(root / 'recovery_amendment_20261015.json'),
        'report_complete': read(root / 'audit.json').get('complete') if (root / 'audit.json').exists() else False}
    save(REPO / 'runs/tb21-e9-agentbrew-evo89-test89-20261012/checkpoint_bounded_recovery_20261015.json', checkpoint)
    print(json.dumps({key: value for key, value in checkpoint.items()
                      if key not in {'teacher_requests', 'amendment'}}, indent=2))


if __name__ == '__main__':
    main()
