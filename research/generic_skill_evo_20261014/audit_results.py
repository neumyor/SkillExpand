"""Audit this experiment's saved artifacts without replaying task execution."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
REMOTE_ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/generic-skill-evolution-20261014')
sys.path[:0] = [str(ROOT / 'code'), str(ROOT.parent / 'SkillExpand/scripts'),
               '/data2/liyishan/SkillExpand-tb21/SkillExpand/scripts']


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def mapped(value):
    path = Path(value)
    if path.is_relative_to(REMOTE_ROOT):
        return ROOT / path.relative_to(REMOTE_ROOT)
    return path if path.is_absolute() else ROOT / path


def normalize(value):
    if isinstance(value, str) and value.startswith(str(REMOTE_ROOT)):
        return str(mapped(value))
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def tokens(metrics):
    metrics = metrics if isinstance(metrics, dict) else {}
    aliases = {'input': ('prompt_tokens', 'input_tokens', 'total_prompt_tokens', 'n_input_tokens'),
               'output': ('completion_tokens', 'output_tokens', 'total_completion_tokens', 'n_output_tokens'),
               'total': ('total_tokens',)}
    result = {}
    for name, keys in aliases.items():
        result[name] = next((metrics[key] for key in keys if isinstance(metrics.get(key), (int, float))
                             and not isinstance(metrics[key], bool)), None)
    if result['total'] is None and result['input'] is not None and result['output'] is not None:
        result['total'] = result['input'] + result['output']
    return result


def aggregate_tokens(rows):
    return {name: {'observed_total': sum(values) if values else None,
                   'known_records': len(values), 'unknown_records': len(rows) - len(values),
                   'complete': bool(rows) and len(values) == len(rows)}
            for name in ('input', 'output', 'total')
            for values in [[row.get('tokens', {}).get(name) for row in rows
                            if row.get('tokens', {}).get(name) is not None]]}


def marker_audit(directory, batch=None):
    problems = []
    try:
        marker_path = directory / 'preaccept_audit.json'
        marker = read(marker_path)
        if marker.get('passed') is not True:
            problems.append('preaccept_not_passed')
        required = {'skill': directory / 'skill.json', 'frozen_input': ROOT / 'frozen/input.json',
                    'executor': ROOT / 'frozen/executor.json', 'protocol': ROOT / 'frozen/protocol.json',
                    'evaluator': ROOT / 'evaluate.py', 'request': directory / 'request.json',
                    'generation_module': ROOT / 'generate.py'}
        roles = {}
        bindings = marker.get('bindings', [])
        if not isinstance(bindings, list):
            raise ValueError('bindings')
        for index, binding in enumerate(bindings):
            role = binding['role']
            current, snapshot = mapped(binding['current']), mapped(binding['snapshot'])
            if role in roles or current.resolve() == snapshot.resolve():
                problems.append('invalid_binding:' + role)
            roles[role] = current.resolve()
            content = current.read_bytes()
            if content != snapshot.read_bytes():
                problems.append('changed_binding:' + role)
            if batch and content != (batch / 'audit_snapshots' / f'{index:03d}.bin').read_bytes():
                problems.append('changed_batch_binding:' + role)
        for role, expected in required.items():
            if roles.get(role) != expected.resolve():
                problems.append('missing_or_wrong_binding:' + role)
        for role in ('request', 'generation_module'):
            if role not in roles:
                problems.append('missing_binding:' + role)
        if batch:
            if marker_path.read_bytes() != (batch / 'preaccept_audit.json').read_bytes():
                problems.append('changed_batch_marker')
            if (directory / 'skill.json').read_bytes() != (batch / 'skill.json').read_bytes():
                problems.append('changed_batch_skill')
    except (OSError, ValueError, KeyError, TypeError):
        problems.append('missing_or_invalid_preaccept_artifacts')
    return {'passed': not problems, 'problems': problems}


def audit_trial(directory, expected_skill, executor, protocol):
    row = {'path': str(directory.relative_to(ROOT)), 'artifact_valid': False,
           'reward': None, 'sandbox_id': None, 'tokens': tokens(None), 'problems': []}
    problems = row['problems']
    try:
        record = read(directory / 'record.json')
        row.update(record_status=record.get('status'), duration_seconds=record.get('duration_seconds'),
                   exception_type=(record.get('exception') or {}).get('exception_type'),
                   error_type=record.get('error_type'), sandbox_id=record.get('sandbox_id'),
                   tokens=tokens(record.get('metrics')))
        if record.get('status') != 'valid':
            problems.append('record_not_valid')
        trial_input = read(directory / 'input.json')
        expected = {'skill': expected_skill, 'executor': executor, 'protocol': protocol,
                    'task_path': str(ROOT / 'frozen/tasks' / protocol['task_name'])}
        if normalize(trial_input) != normalize(expected):
            problems.append('trial_input_mismatch')
        result_path = mapped(record['result_path'])
        if not result_path.is_relative_to(directory):
            problems.append('result_outside_trial')
        snapshot_path = result_path.parent / 'snapshot.json'
        if snapshot_path.exists():
            row['sandbox_id'] = read(snapshot_path).get('source_sandbox_id')
        trace_path = result_path.parent / 'agent/trajectory.json'
        if trace_path.exists():
            row['tokens'] = tokens(read(trace_path).get('final_metrics'))
        # This validator reads official stdout internally; its contents never enter reports.
        from summarize_tb21_empirical import validate_trial
        result, reward, trace_path = validate_trial(result_path, protocol['task_name'],
                                                   activation=True, model=protocol['model_name'])
        row['reward'] = reward
        if record.get('reward') != reward or isinstance(record.get('reward'), bool):
            problems.append('record_reward_mismatch')
        actual = result['config']
        for key in ('agent', 'verifier'):
            if normalize(actual[key]) != normalize(executor[key]):
                problems.append(key + '_configuration_drift')
        environment = json.loads(json.dumps(executor['environment']))
        environment['kwargs']['skill_root'] = str(directory / 'skills')
        if normalize(actual['environment']) != normalize(environment):
            problems.append('environment_configuration_drift')
        if normalize(actual['task']) != normalize({**executor['task'], 'path': expected['task_path']}):
            problems.append('task_configuration_drift')
        activation = read(result_path.parent / 'agent/skill_activation.json')
        source = mapped(activation['source_path'])
        if not source.is_relative_to(directory / 'skills'):
            problems.append('mounted_source_outside_trial')
        from skillexpand import schema as S
        from skillexpand.skill_package import entrypoint_content
        skill = S.from_dict(S.Skill, expected_skill)
        if source.read_text(encoding='utf-8') != entrypoint_content(skill, package=skill.package_mode):
            problems.append('mounted_entrypoint_mismatch')
        expected_files = {'SKILL.md': source.read_bytes()}
        if skill.package_mode:
            expected_files.update({name: content.encode('utf-8') for name, content in skill.resources.items()})
        actual_files = {path.relative_to(source.parent).as_posix(): path.read_bytes()
                        for path in source.parent.rglob('*') if path.is_file()}
        if actual_files != expected_files:
            problems.append('mounted_resources_mismatch')
        snapshot = read(result_path.parent / 'snapshot.json')
        row['sandbox_id'] = snapshot.get('source_sandbox_id')
        if not row['sandbox_id'] or record.get('sandbox_id') != row['sandbox_id']:
            problems.append('sandbox_identity_missing_or_mismatched')
        trace = read(trace_path)
        row['tokens'] = tokens(trace.get('final_metrics'))
        row['exception_type'] = (result.get('exception_info') or {}).get('exception_type')
    except Exception as exc:
        problems.append('artifact_check_failed:' + type(exc).__name__)
    row['artifact_valid'] = not problems
    return row


def build_audit():
    protocol, executor = read(ROOT / 'frozen/protocol.json'), read(ROOT / 'frozen/executor.json')
    candidates, generations, trials, batches = [], [], [], []
    for directory in sorted(ROOT.glob('development/candidate-*')):
        candidate = {'path': str(directory.relative_to(ROOT)), 'generated': (directory / 'skill.json').exists()}
        candidates.append(candidate)
        for attempt in sorted(directory.glob('generation_attempts/*')):
            metadata = read(attempt / 'generation.json') if (attempt / 'generation.json').exists() else {}
            error = read(attempt / 'error.json') if (attempt / 'error.json').exists() else None
            outcome = read(attempt / 'outcome.json') if (attempt / 'outcome.json').exists() else None
            usage = metadata.get('usage')
            if usage is None and (attempt / 'completion.json').exists():
                usage = read(attempt / 'completion.json').get('usage')
            generations.append({'path': str(attempt.relative_to(ROOT)),
                                'status': 'failed' if error or outcome and not outcome.get('candidate')
                                else 'completed' if outcome else 'pending',
                                'outcome_reason': outcome.get('reason') if outcome else None,
                                'error': error, 'seconds': metadata.get('seconds', (error or {}).get('seconds')),
                                'tokens': tokens(usage)})
        skill = read(directory / 'skill.json') if candidate['generated'] else None
        for trial in sorted(directory.glob('dev-batch-*/trial-*')):
            trials.append(audit_trial(trial, skill, executor, protocol))
    for batch in sorted(ROOT.glob('acceptance/batch-*')):
        row = {'path': str(batch.relative_to(ROOT)), 'passed': False, 'problems': []}
        try:
            number = read(batch / 'candidate.json')['candidate']
            row['recorded_summary'] = read(batch / 'summary.json') if (batch / 'summary.json').exists() else {}
            candidate = ROOT / f'development/candidate-{number:03d}'
            row['candidate'] = number
            row['preaccept'] = marker_audit(candidate, batch)
            skill = read(batch / 'skill.json')
            batch_trials = [audit_trial(batch / f'{i:02d}', skill, executor, protocol) for i in range(1, 6)]
            trials.extend(batch_trials)
            row['trial_paths'] = [trial['path'] for trial in batch_trials]
            if not row['preaccept']['passed']:
                row['problems'].append('preaccept_failed')
        except (OSError, ValueError, KeyError, TypeError):
            row['problems'].append('missing_or_invalid_batch_artifacts')
        batches.append(row)
    counts = Counter(trial['sandbox_id'] for trial in trials if trial['sandbox_id'])
    excluded = set(protocol['excluded_sandbox_ids'])
    if (ROOT / 'original_environment_ids.json').exists():
        excluded.update(row['sandbox_id'] for row in read(ROOT / 'original_environment_ids.json')
                        if row.get('sandbox_id'))
    for trial in trials:
        identity = trial['sandbox_id']
        trial['fresh_unique_sandbox'] = bool(identity) and identity not in excluded and counts[identity] == 1
        if identity and not trial['fresh_unique_sandbox']:
            trial['problems'].append('sandbox_reused_or_historical')
            trial['artifact_valid'] = False
    by_path = {trial['path']: trial for trial in trials}
    for batch in batches:
        rows = [by_path[path] for path in batch.get('trial_paths', [])]
        batch['valid_trials'] = sum(row['artifact_valid'] for row in rows)
        batch['successful_trials'] = sum(row['artifact_valid'] and row['reward'] == 1 for row in rows)
        summary = batch.get('recorded_summary', {})
        if summary.get('candidate') != batch.get('candidate') or summary.get('attempts') != 5:
            batch['problems'].append('missing_or_inconsistent_batch_summary')
        batch['passed'] = not batch['problems'] and len(rows) == 5 and all(
            row['artifact_valid'] and row['reward'] == 1 and row['fresh_unique_sandbox'] for row in rows)
        if batch['passed'] and (summary.get('passed') is not True or summary.get('valid') != 5
                                or summary.get('successes') != 5
                                or summary.get('fresh_independent_environments') is not True
                                or summary.get('sandbox_ids') != [row['sandbox_id'] for row in rows]):
            batch['problems'].append('batch_summary_disagrees_with_artifacts')
            batch['passed'] = False
    return {'format': 'generic-skill-posttrial-audit-v1', 'root': str(ROOT),
            'task_name': protocol['task_name'], 'model_name': protocol['model_name'],
            'old_research_result_credit': 0, 'candidates': candidates, 'generation_attempts': generations,
            'trials': trials, 'acceptance_batches': batches,
            'generation_tokens': aggregate_tokens(generations), 'executor_tokens': aggregate_tokens(trials),
            'accepted': any(batch['passed'] for batch in batches)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'artifact_audit.json')
    args = parser.parse_args()
    result = build_audit()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'accepted': result['accepted'], 'trials': len(result['trials']), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
