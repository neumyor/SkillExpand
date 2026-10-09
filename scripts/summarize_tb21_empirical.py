"""Audit verifier-backed TB2.1 slots and report closed-set empirical scores."""
import argparse
import json
from collections import Counter
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def validate_trial(result_path, task_name, *, activation=False, model='qwen3.6-flash-distill'):
    result_path = Path(result_path)
    if not result_path.is_file():
        raise ValueError('missing_result')
    result = read(result_path)
    if result.get('task_name') != task_name:
        raise ValueError('task_identity_mismatch')
    exception = result.get('exception_info')
    if exception:
        raise ValueError('trial_exception:' + str(exception.get('exception_type', 'unknown')))
    reward = (result.get('verifier_result') or {}).get('rewards', {}).get('reward')
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or reward not in (0, 1):
        raise ValueError('missing_or_invalid_verifier_reward')
    trajectory = result_path.parent / 'agent/trajectory.json'
    if not trajectory.is_file() or not read(trajectory):
        raise ValueError('missing_trajectory')
    agent = result.get('config', {}).get('agent', {})
    if agent.get('model_name') != 'openai/' + model.removeprefix('openai/'):
        raise ValueError('executor_model_mismatch')
    if activation:
        activated = read(result_path.parent / 'agent/skill_activation.json')
        if activated.get('load_stage') != 'after_selection':
            raise ValueError('missing_skill_activation')
        if not str(agent.get('import_path', '')).endswith(':TencentSkillTerminus2'):
            raise ValueError('executor_skill_adapter_mismatch')
    return result, int(reward), str(trajectory)


def execution_settings(result):
    agent = result.get('config', {}).get('agent', {})
    return {k: agent.get(k) for k in ('model_name', 'override_timeout_sec')} | {
        'llm_call_kwargs': agent.get('kwargs', {}).get('llm_call_kwargs'),
        'api_base': agent.get('kwargs', {}).get('api_base'),
    }


def metrics(rows, tasks, attempts=3):
    grouped = {task: [] for task in range(tasks)}
    seen = set()
    for row in rows:
        identity = (row['task_id'], row['attempt_index'])
        if identity in seen or identity[0] not in grouped or not 1 <= identity[1] <= attempts:
            raise ValueError('duplicate_or_invalid_attempt_identity')
        seen.add(identity)
        grouped[identity[0]].append(row)
    complete = all(len(group) == attempts for group in grouped.values())
    per_task = [{'task_id': task, 'attempts': len(group),
                 'successes': sum(r['reward'] for r in group),
                 'pass_at_3': bool(any(r['reward'] for r in group)) if len(group) == attempts else None}
                for task, group in grouped.items()]
    return {'complete': complete, 'valid_attempts': len(rows),
            'expected_attempts': tasks * attempts,
            'pass_at_1': sum(r['reward'] for r in rows) / (tasks * attempts) if complete else None,
            'pass_at_3': sum(t['pass_at_3'] for t in per_task) / tasks if complete else None,
            'per_task': per_task}


def summarize(root):
    root = Path(root)
    manifest = read(root / 'manifest.json')
    library = {s['skill_id']: s for s in read(root / 'library.json')}
    rows, rejected = [], []
    settings = []
    for path in sorted((root / 'slots').glob('*/record.json')):
        row = read(path)
        try:
            if row.get('status') != 'valid':
                raise ValueError(row.get('error_class', 'unresolved_slot'))
            task, attempt = row['task_id'], row['attempt_index']
            if path.parent.name != f'{task:02d}-{attempt}':
                raise ValueError('slot_directory_identity_mismatch')
            if manifest['task_names'][task] != row['task_name']:
                raise ValueError('task_identity_mismatch')
            selection = row['selection']
            expected_catalog = [{'skill_id': s['skill_id'], 'description': s['description']}
                                for s in sorted(library.values(), key=lambda s: s['skill_id'])]
            if selection['catalog'] != expected_catalog or not selection['ok']:
                raise ValueError('catalog_identity_mismatch')
            skill = library[selection['loaded_skill_id']]
            if (selection['skill_id'] != skill['skill_id'] or
                    selection['loaded_skill_key'] != f"{skill['skill_id']}@v{skill['version']}" or
                    selection['load_stage'] != 'after_selection'):
                raise ValueError('skill_identity_mismatch')
            result, reward, trajectory = validate_trial(row['result_path'], row['task_name'], activation=True)
            activated = read(Path(row['result_path']).parent / 'agent/skill_activation.json')
            mounted = Path(activated['source_path'])
            if mounted.resolve() != Path(row['skill_file']).resolve() or mounted.read_text() != skill['body']:
                raise ValueError('mounted_skill_body_mismatch')
            if reward != row['reward'] or trajectory != row['trajectory_path']:
                raise ValueError('result_record_mismatch')
            rows.append(row)
            settings.append(execution_settings(result))
        except (ValueError, KeyError, OSError, TypeError) as exc:
            rejected.append({'slot': path.parent.name, 'error': str(exc)})
    empirical = metrics(rows, manifest['tasks'])
    baseline_path = root / 'baseline.json'
    baseline = read(baseline_path) if baseline_path.exists() else {'metrics': None, 'settings': []}
    comparable = (empirical['complete'] and (baseline.get('metrics') or {}).get('complete', False)
                  and bool(settings) and all(s == settings[0] for s in settings)
                  and bool(baseline['settings']) and all(s == settings[0] for s in baseline['settings']))
    failures = Counter()
    for path in (root / 'slots').glob('*/requests/*/error.json'):
        failures[read(path)['error_class']] += 1
    report = {'scope': '89-task closed-set empirical execution; no independent test generalization',
              'empirical': empirical, 'baseline': baseline.get('metrics'),
              'baseline_comparable': comparable, 'delta': None,
              'infrastructure_failure_counts': dict(failures), 'rejected_slots': rejected,
              'gate_or_evolution_changed': False}
    if comparable:
        report['delta'] = {k: empirical[k] - baseline['metrics'][k] for k in ('pass_at_1', 'pass_at_3')}
        for task in empirical['per_task']:
            task['baseline_successes'] = baseline['metrics']['per_task'][task['task_id']]['successes']
    if len({r['result_path'] for r in rows}) != len(rows):
        raise ValueError('result_path_reused_across_slots')
    write(root / 'result.json', report)
    write(root / 'audit.json', {'complete': empirical['complete'] and not rejected,
        'valid_slots': len(rows), 'expected_slots': 267, 'rejected_slots': rejected,
        'unique_result_paths': len({r['result_path'] for r in rows}),
        'baseline_comparable': comparable})
    score = lambda value: 'pending' if value is None else f'{value:.2%}'
    stage = manifest.get('stage', 'E1_empirical').removesuffix('_empirical')
    lines = [f'# {stage} frozen-library empirical evaluation', '', report['scope'], '',
             f"Library source: {manifest.get('source_kind', 'evolved_final_library')}.",
             f"Valid attempts: {len(rows)}/267. Completion: {empirical['complete'] and not rejected}.",
             f"pass@1: {score(empirical['pass_at_1'])}; pass@3: {score(empirical['pass_at_3'])}.",
             f'Baseline comparable: {comparable}.',
             f'Infrastructure failures (excluded from rewards): {dict(failures)}.', '',
             '| Task | Valid Attempts | Successes | Baseline Successes |',
             '| --- | --- | --- | --- |']
    for task in empirical['per_task']:
        lines.append(f"| {manifest['task_names'][task['task_id']]} | {task['attempts']} | "
                     f"{task['successes']} | {task.get('baseline_successes', 'not compared')} |")
    (root / 'report.md').write_text('\n'.join(lines) + '\n')
    status_path = root / 'status.json'
    if status_path.exists():
        status = read(status_path)
        if status.get('status') == 'running':
            status.update(coverage=f'{len(rows)}/267',
                          phase='full_panel' if (root / 'canary.json').exists() else 'canary')
            write(status_path, status)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True, type=Path)
    args = parser.parse_args()
    report = summarize(args.run_dir)
    print(json.dumps({k: report[k] for k in ('baseline_comparable', 'delta', 'rejected_slots')}))
