"""Evaluate frozen skill candidates; generation and audit are separate actions."""
import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/generic-skill-evolution-20261014')
RESULT_LIMIT = 8192


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def freeze_bytes(path, content):
    path = Path(path)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f'Frozen bytes changed: {path}')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def bounded_content(content):
    if not isinstance(content, str):
        raise ValueError('Tool result content must be a string')
    size = len(content)
    return {'content': content if size <= RESULT_LIMIT else content[:4096] + content[-4096:],
            'original_characters': size, 'truncated': size > RESULT_LIMIT,
            'head_characters': min(size, 4096) if size > RESULT_LIMIT else size,
            'tail_characters': 4096 if size > RESULT_LIMIT else 0}


def project_tool_results(trace):
    observations = []
    for index, step in enumerate(trace['steps']):
        calls = step.get('tool_calls') or []
        observation = step.get('observation')
        if not calls and observation is None:
            continue
        results = [] if observation is None else observation.get('results', [])
        observations.append({
            'step_index': index, 'step_id': step.get('step_id'),
            'tool_functions': [call.get('function_name') for call in calls],
            'observation_present': observation is not None,
            'results': [{'result_index': result_index, **bounded_content(result.get('content', ''))}
                        for result_index, result in enumerate(results)],
        })
    return observations


def feedback(directory, record):
    write(directory / 'feedback.json', {
        'status': record['status'], 'reward': record.get('reward'),
        'tool_observations': record.get('tool_observations', []),
        'error_type': record.get('error_type'), 'error': record.get('error'),
        'exception': record.get('exception'),
        'source_trajectory': record.get('trajectory_path'),
        'projection': 'tool-results-head-tail-8192-v1',
    })


def manifest():
    value = read(ROOT / 'frozen/protocol.json')
    name = value['task_name']
    if not isinstance(name, str) or not name or Path(name).name != name or name in ('.', '..'):
        raise ValueError('Invalid task identity in protocol')
    if not isinstance(value.get('excluded_sandbox_ids'), list):
        raise ValueError('Protocol must freeze historical sandbox exclusions')
    return value


def skill_for(directory):
    from skillexpand import schema as S
    skill = S.from_dict(S.Skill, read(directory / 'skill.json'))
    if not skill.body.strip():
        raise ValueError('Candidate skill body is empty')
    return skill


def verify_audit(directory):
    audit_path = directory / 'preaccept_audit.json'
    audit = read(audit_path)
    if audit.get('passed') is not True:
        raise ValueError('Preaccept audit has not passed')
    required = {
        'skill': directory / 'skill.json',
        'frozen_input': ROOT / 'frozen/input.json',
        'executor': ROOT / 'frozen/executor.json',
        'protocol': ROOT / 'frozen/protocol.json',
        'evaluator': Path(__file__).resolve(),
    }
    roles = {}
    bindings = audit.get('bindings', [])
    if not isinstance(bindings, list):
        raise ValueError('Audit bindings must be a list')
    for binding in bindings:
        role = binding['role']
        if role in roles:
            raise ValueError('Duplicate audit binding role: ' + role)
        current, snapshot = (ROOT / binding[key] for key in ('current', 'snapshot'))
        current, snapshot = current.resolve(), snapshot.resolve()
        if current == snapshot:
            raise ValueError('Audit snapshot must be a separate file')
        if current.read_bytes() != snapshot.read_bytes():
            raise ValueError('Audit binding changed: ' + role)
        roles[role] = current
    for role, path in required.items():
        if roles.get(role) != path.resolve():
            raise ValueError('Missing or incorrect audit binding: ' + role)
    for role in ('request', 'generation_module'):
        if role not in roles:
            raise ValueError('Missing audit binding: ' + role)
    return audit_path, bindings


def run_trial(directory, skill):
    from omegaconf import OmegaConf
    from skillexpand.benchmarks.terminalbench import harbor_rollout
    from skillexpand.schema import to_dict
    from summarize_tb21_empirical import validate_trial

    protocol = manifest()
    frozen = ROOT / 'frozen'
    executor = read(frozen / 'executor.json')
    task_name = protocol['task_name']
    task_path = str(frozen / 'tasks' / task_name)
    trial_input = {'skill': to_dict(skill), 'executor': executor, 'task_path': task_path,
                   'protocol': protocol}
    if (directory / 'input.json').exists() and read(directory / 'input.json') != trial_input:
        raise ValueError('Existing trial input differs from frozen candidate or executor')
    if (directory / 'record.json').exists():
        if not (directory / 'input.json').exists():
            raise ValueError('Unaudited existing trial')
        return read(directory / 'record.json')
    runtime_path = frozen / 'runtime'
    os.environ.update(MODEL_NAME=protocol['model_name'], DATASET_PATH=str(frozen / 'tasks'),
                      TENCENT_ADAPTER_PYTHON=str(runtime_path / 'framework'),
                      OPENAI_API_KEY=os.environ['TB21_EXECUTOR_API_KEY'])
    rollout_config = dict(protocol['rollout'])
    rollout_config['runner_script'] = str(ROOT / rollout_config['runner_script'])
    cfg = OmegaConf.create({'benchmark': {'task_file': str(frozen / 'task_table.json'),
                                         'rollout': rollout_config}})
    table = read(frozen / 'task_table.json')
    if len(table) != 1 or table[0]['task_name'] != task_name:
        raise ValueError('Task table differs from protocol identity')
    started = time.time()
    directory.mkdir(parents=True, exist_ok=True)
    write(directory / 'input.json', trial_input)
    write(directory / 'started.json', {'at': started, 'skill': skill.key})
    record = {'status': 'unresolved', 'tool_observations': []}
    try:
        rollout, runtime = harbor_rollout(cfg, 0, skill, 1, directory)
        write(directory / 'rollout.json', {'rollout': rollout, 'runtime': runtime})
        if len(rollout['trials']) != 1:
            raise ValueError('Expected exactly one official trial')
        trial = rollout['trials'][0]
        result_path = Path(trial['result_path'])
        trace_path = result_path.parent / 'agent/trajectory.json'
        record.update(result_path=str(result_path), trajectory_path=str(trace_path))
        if trace_path.exists():
            record['tool_observations'] = project_tool_results(read(trace_path))
        snapshot = read(result_path.parent / 'snapshot.json')
        identity = snapshot.get('source_sandbox_id')
        record.update(sandbox_id=identity, snapshot_status=snapshot.get('status'))
        result, reward, verified_trace = validate_trial(str(result_path), task_name, activation=True)
        actual = result['config']
        if actual['agent'] != executor['agent']:
            raise ValueError('Executor configuration drift')
        if actual['verifier'] != executor['verifier']:
            raise ValueError('Verifier configuration drift')
        expected_env = {**executor['environment'], 'kwargs': dict(executor['environment']['kwargs'])}
        expected_env['kwargs']['skill_root'] = str(directory / 'skills')
        if actual['environment'] != expected_env:
            raise ValueError('Environment configuration drift')
        if actual['task'] != {**executor['task'], 'path': task_path}:
            raise ValueError('Task configuration drift')
        if not identity:
            raise ValueError('Missing authoritative sandbox identity')
        trace = read(verified_trace)
        record.update(status='valid', reward=reward, trajectory_path=str(verified_trace),
                      tool_observations=project_tool_results(trace),
                      exception=result.get('exception_info'), metrics=trace.get('final_metrics'),
                      tool_calls=sum(len(step.get('tool_calls') or []) for step in trace['steps']))
    except Exception as exc:
        message = str(exc)
        for name in ('OPENAI_API_KEY', 'E2B_API_KEY', 'TB21_EXECUTOR_API_KEY', 'TB21_METHOD_API_KEY'):
            if os.environ.get(name):
                message = message.replace(os.environ[name], '[REDACTED]')
        record.update(status='unresolved', error_type=type(exc).__name__, error=message)
    record['duration_seconds'] = time.time() - started
    write(directory / 'record.json', record)
    print(json.dumps({'directory': str(directory), 'status': record['status'],
                      'reward': record.get('reward'), 'duration': record['duration_seconds']}), flush=True)
    return record


def previous_sandbox_ids(exclude=None):
    identities = set(manifest()['excluded_sandbox_ids'])
    for record_path in ROOT.glob('**/record.json'):
        if 'frozen' in record_path.relative_to(ROOT).parts or (exclude and exclude in record_path.parents):
            continue
        record = read(record_path)
        identity = record.get('sandbox_id')
        if not identity and record.get('result_path'):
            snapshot_path = Path(record['result_path']).parent / 'snapshot.json'
            if snapshot_path.exists():
                identity = read(snapshot_path).get('source_sandbox_id')
        if identity:
            identities.add(identity)
    return identities


def report():
    trials = [{'path': str(path), **read(path)} for path in sorted(ROOT.glob('**/record.json'))
              if 'frozen' not in path.relative_to(ROOT).parts]
    batches = [{'path': str(path), **read(path)} for path in sorted(ROOT.glob('acceptance/*/summary.json'))]
    write(ROOT / 'results.json', {'trials': trials, 'acceptance_batches': batches})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('dev', 'accept', 'report'))
    parser.add_argument('--candidate', type=int, default=1)
    parser.add_argument('--batch', type=int, default=1)
    parser.add_argument('--runs', type=int, default=3, choices=(3,))
    args = parser.parse_args()
    if args.candidate < 1 or args.batch < 1:
        parser.error('candidate and batch must be positive')
    if args.action == 'report':
        report()
        return
    directory = ROOT / f'development/candidate-{args.candidate:03d}'
    skill = skill_for(directory)
    if args.action == 'dev':
        def develop(attempt):
            trial_dir = directory / f'dev-batch-{args.batch:03d}/trial-{attempt:03d}'
            record = run_trial(trial_dir, skill)
            feedback(trial_dir, record)
            return record
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(develop, range(1, 4)))
    else:
        audit_path, bindings = verify_audit(directory)
        batch = ROOT / f'acceptance/batch-{args.batch:03d}'
        freeze_bytes(batch / 'skill.json', (directory / 'skill.json').read_bytes())
        freeze_bytes(batch / 'preaccept_audit.json', audit_path.read_bytes())
        for index, binding in enumerate(bindings):
            freeze_bytes(batch / 'audit_snapshots' / f'{index:03d}.bin',
                         (ROOT / binding['current']).read_bytes())
        freeze_bytes(batch / 'candidate.json', json.dumps({'candidate': args.candidate}).encode('utf-8'))
        skill = skill_for(batch)
        prior_ids = previous_sandbox_ids(exclude=batch)
        with ThreadPoolExecutor(max_workers=5) as pool:
            records = list(pool.map(lambda attempt: run_trial(batch / f'{attempt:02d}', skill), range(1, 6)))
        identities = [record.get('sandbox_id') for record in records]
        prior_ids.update(previous_sandbox_ids(exclude=batch))
        fresh = (all(identities) and len(set(identities)) == 5
                 and not prior_ids.intersection(identities))
        # Recheck audited bytes after execution; changed modules invalidate acceptance.
        verify_audit(directory)
        freeze_bytes(batch / 'skill.json', (directory / 'skill.json').read_bytes())
        freeze_bytes(batch / 'preaccept_audit.json', audit_path.read_bytes())
        for index, binding in enumerate(bindings):
            freeze_bytes(batch / 'audit_snapshots' / f'{index:03d}.bin',
                         (ROOT / binding['current']).read_bytes())
        write(batch / 'summary.json', {
            'candidate': args.candidate, 'attempts': 5,
            'valid': sum(record['status'] == 'valid' for record in records),
            'successes': sum(record.get('reward') == 1 for record in records),
            'sandbox_ids': identities, 'fresh_independent_environments': bool(fresh),
            'passed': bool(fresh) and all(record['status'] == 'valid' and record.get('reward') == 1
                                         for record in records),
        })
    report()


if __name__ == '__main__':
    main()
