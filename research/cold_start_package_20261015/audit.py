"""Offline four-slot audit, reusing the loading study's native trial replay."""
import argparse
from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import sys


def read(path):
    return json.loads(Path(path).read_text())


def audit(root, loading_audit):
    spec = importlib.util.spec_from_file_location('_package_loading_audit', loading_audit)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    replay = object.__new__(module.LoadingAudit)
    replay.root = Path(root).resolve() / 'execution'
    replay.remote = replay.root
    replay.phase = 'mechanism_smoke'
    replay.manifest = read(replay.root / 'manifest.json')
    replay.native = module.NativeReplay(replay.root)
    replay.libraries = {name: read(replay.root / 'frozen/libraries' / (name + '.json'))
                        for name in ('old', 'evolved')}
    replay.table = {row['task_name']: row for row in read(replay.root / 'frozen/task_table.json')}
    replay.schedule = read(replay.root / 'frozen/schedule.json')
    replay.template = read(replay.root / 'frozen/executor_template.json')
    replay.excluded = set(read(replay.root / 'frozen/historical_sandbox_ids.json'))
    replay.errors, replay.pending, replay.source_limits = [], [], []
    replay.trials, replay.identities = [], defaultdict(list)
    generation_calls, payloads = 0, []
    for arm in ('old', 'new'):
        folder = Path(root) / 'generations' / arm
        outcome, request = read(folder / 'outcome.json'), read(folder / 'request.json')
        generation_calls += outcome['calls']
        if outcome['status'] != 'valid' or outcome['calls'] != 1:
            replay.error(folder, 'generation_status_or_budget_drift')
        payload = json.loads(request['messages'][0]['content'].rsplit('\n', 1)[1])
        payloads.append(payload)
        projection = read(Path(root) / 'frozen/generation_projection.json')
        if payload['cards'] != [projection]:
            replay.error(folder, 'generation_evidence_drift')
    if payloads[0] != payloads[1] or generation_calls != 2:
        replay.error(Path(root), 'generation_comparison_or_budget_drift')
    bindings = read(replay.root / 'bindings.json')
    for binding in bindings:
        current, snapshot = [replay.root / binding[key] for key in ('current', 'snapshot')]
        if current.read_bytes() != snapshot.read_bytes():
            replay.error(current, 'frozen_bytes_changed')
    expected = [{'cell': cell, 'task': task, 'attempt': attempt}
                for attempt in (1, 2) for cell in ('old_autonomous', 'evolved_autonomous')
                for task in replay.table]
    replay.same(replay.root / 'frozen/schedule.json', replay.schedule, expected, 'schedule_drift')
    if len(expected) != 4 or replay.manifest['executor_max'] != 4:
        replay.error(replay.root / 'manifest.json', 'four_slot_budget_drift')
    for arm, library in [('old', 'old'), ('new', 'evolved')]:
        original = Path(root) / 'generations' / arm / 'library.json'
        if original.read_bytes() != (replay.root / 'frozen/libraries' / (library + '.json')).read_bytes():
            replay.error(original, 'generated_library_changed')
    if (replay.root / 'integrity_abort.json').exists():
        replay.error(replay.root, 'identity_integrity_abort')
    for row in replay.schedule:
        replay.trial(row)
    allowed = {replay.root / row['path'] for row in replay.trials}
    reserved = list((replay.root / replay.phase / 'slots').rglob('reserved.json'))
    if len(reserved) != 4 or any(path.parent not in allowed for path in reserved):
        replay.error(replay.root, 'reservation_budget_drift')
    for identity, rows in replay.identities.items():
        if len(rows) != 1:
            replay.error(replay.root, 'duplicate_sandbox_identity')
    common = [row['common_agent'] for row in replay.trials if row.get('common_agent')]
    if common and any(agent != common[0] for agent in common[1:]):
        replay.error(replay.root, 'executor_configuration_differs')
    summary = {
        'status': 'passed' if not replay.errors and not replay.pending else 'needs_attention',
        'generation_calls': generation_calls, 'identical_evidence_payload': payloads[0] == payloads[1],
        'frozen_bindings': len(bindings), 'reserved_slots': len(reserved),
        'actual_trials': sum(row['actual_harbor_trials'] for row in replay.trials),
        'unique_sandbox_ids': len(replay.identities),
        'errors': replay.errors, 'pending': replay.pending, 'source_limits': replay.source_limits,
        'arms': {label: module.aggregate([row for row in replay.trials if row['cell'] == cell])
                 for label, cell in [('old', 'old_autonomous'), ('new_initial', 'evolved_autonomous')]},
        'trials': replay.trials,
        'interpretation': 'Mechanism smoke on the source task; no statistical or causal improvement claim.'}
    (Path(root) / 'execution_audit.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in summary.items() if key != 'trials'}, ensure_ascii=False))
    return 0 if summary['status'] == 'passed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--loading-audit', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(audit(args.root, args.loading_audit))
