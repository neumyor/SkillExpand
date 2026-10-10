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
    if exception and exception.get('exception_type') != 'AgentTimeoutError':
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
    if manifest.get('source_kind') == 'aligned_evolved_final_library':
        if {k: f"{k}@v{s['version']}" for k, s in library.items()} != manifest['library_versions']:
            raise ValueError('Frozen library versions differ from manifest')
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
            result, reward, trajectory = validate_trial(row['result_path'], row['task_name'], activation=True,
                model=manifest.get('model', 'qwen3.6-flash-distill'))
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
    historical_errors = Counter()
    for path in (root / 'slots').glob('*/requests/*/error.json'):
        category = read(path)['error_class']
        historical_errors[category] += 1
        results = list(path.parent.glob('jobs/*/*/result.json'))
        if len(results) == 1:
            outcome = read(results[0])
            error = outcome.get('exception_info') or {}
            reward = (outcome.get('verifier_result') or {}).get('rewards', {}).get('reward')
            if (error.get('exception_type') == 'AgentTimeoutError' and
                    not isinstance(reward, bool) and isinstance(reward, (int, float)) and reward in (0, 1)):
                continue
        failures[category] += 1
    report = {'scope': '89-task closed-set empirical execution; no independent test generalization',
              'empirical': empirical, 'baseline': baseline.get('metrics'),
              'baseline_comparable': comparable, 'delta': None,
              'infrastructure_failure_counts': dict(failures), 'rejected_slots': rejected,
              'gate_or_evolution_changed': False}
    report['historical_request_error_counts'] = dict(historical_errors)
    report['accepted_agent_timeout_slots'] = sum(
        (read(row['result_path']).get('exception_info') or {}).get('exception_type') == 'AgentTimeoutError'
        for row in rows)
    if comparable:
        report['delta'] = {k: empirical[k] - baseline['metrics'][k] for k in ('pass_at_1', 'pass_at_3')}
        for task in empirical['per_task']:
            task['baseline_successes'] = baseline['metrics']['per_task'][task['task_id']]['successes']
    if manifest.get('initial_panel'):
        source = Path(manifest['initial_panel'])
        original, original_settings = [], []
        source_identity = read(source / 'alignment.json')
        for path in sorted((source / 'slots').glob('*/record.json')):
            row = read(path)
            outcome, reward, _ = validate_trial(row['result_path'], row['task_name'], activation=True,
                                               model=source_identity['executor_model'])
            original.append({**row, 'reward': reward})
            original_settings.append(execution_settings(outcome))
        before = metrics(original, manifest['tasks'])
        task_match = read(source / 'tasks.json') == read(root / 'tasks.json')
        roles_match = (manifest['model'] == source_identity['executor_model'] and
                       manifest['selector_model'] == manifest['initial_panel_selector'])
        settings_match = bool(settings) and all(s == settings[0] for s in settings + original_settings)
        report['initial_panel_comparison'] = {
            'source': str(source), 'tasks_match': task_match, 'roles_match': roles_match,
            'execution_settings_match': settings_match, 'initial': before,
            'paired_comparable': task_match and roles_match and settings_match and before['complete'] and empirical['complete'],
            'infrastructure_change': 'Final evaluation uses internal mirror/DNS and pipeline v6 cache.',
            'delta': {k: empirical[k] - before[k] for k in ('pass_at_1', 'pass_at_3')}
                     if task_match and empirical['complete'] and before['complete'] else None,
            'per_task': [{'task_id': t['task_id'], 'initial_successes': before['per_task'][t['task_id']]['successes'],
                          'final_successes': t['successes']} for t in empirical['per_task']] if task_match else [],
        }
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
             f"Accepted AgentTimeoutError slots with real verifier rewards: {report['accepted_agent_timeout_slots']}.",
             'Each slot uses its earliest verifier-valid request; later retries remain historical evidence.', '',
             '| Task | Valid Attempts | Successes | Baseline Successes |',
             '| --- | --- | --- | --- |']
    for task in empirical['per_task']:
        lines.append(f"| {manifest['task_names'][task['task_id']]} | {task['attempts']} | "
                     f"{task['successes']} | {task.get('baseline_successes', 'not compared')} |")
    if 'initial_panel_comparison' in report:
        comparison = report['initial_panel_comparison']
        lines.extend(['', '## Initial Library Comparison', '',
            f"Paired comparable: {comparison['paired_comparable']}; role match: {comparison['roles_match']}.",
            comparison['infrastructure_change'],
            f"Descriptive delta: {comparison['delta']}. No independent generalization or significance claim."])
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
