"""Audit bare Qwen trials without counting missing verifier outcomes as zero."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def trial(path, current=True):
    raw = read(path)
    root = path.parent
    agent = raw.get('config', {}).get('agent', {})
    reward = ((raw.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    exception = (raw.get('exception_info') or {}).get('exception_type')
    issues = []
    name = raw.get('task_name')
    trial_name = root.parent.name if root.name.startswith('execution-') else root.name
    if not name or raw.get('trial_name') != trial_name or not trial_name.startswith(name + '__'):
        issues.append('identity')
    if agent.get('model_name') != 'openai/qwen3.6-flash-distill':
        issues.append('model')
    if not read(root / 'agent/trajectory.json'):
        issues.append('trajectory')
    if reward not in (0, 1):
        issues.append('missing_reward')
    if exception and exception != 'AgentTimeoutError':
        issues.append('execution_exception')
    try:
        if float((root / 'verifier/reward.txt').read_text().strip()) != reward:
            issues.append('reward_file')
    except (OSError, ValueError):
        issues.append('reward_file')
    stdout = root / 'verifier/test-stdout.txt'
    log = stdout.read_text(errors='replace') if stdout.exists() else ''
    if 'test session starts' not in log or not re.search(r'\b[1-9]\d* (?:passed|failed)\b', log):
        issues.append('tests_not_executed')
    request_count = 0
    if current:
        effective = read(root / 'agent/effective_agent_config.json')
        if effective != {'max_input_tokens': 262144, 'max_output_tokens': 65536,
                         'enable_summarize': True, 'proactive_summarization_threshold': 8000,
                         'skill': None}:
            issues.append('effective_context')
        if raw.get('config', {}).get('mounts') or agent.get('kwargs', {}).get('skills_dir'):
            issues.append('skill_mount')
        network = read(root / 'package_network.json')
        if network.get('dns') != '183.60.83.19' or network.get('mirror') != 'https://mirrors.tencentyun.com':
            issues.append('package_network')
        req = root / 'agent/raw_model_requests.jsonl'
        if req.exists():
            with req.open() as source:
                for line in source:
                    if not line.strip():
                        continue
                    body = json.loads(line)
                    request_count += 1
                    if (body.get('model') != 'qwen3.6-flash-distill'
                            or body.get('max_tokens') != 65536 or body.get('top_p') != .95
                            or body.get('top_k') != 20 or body.get('enable_thinking') is not True
                            or body.get('chat_template_kwargs', {}).get('preserve_thinking') is not True
                            or 'reasoning_effort' in body):
                        issues.append('http_parameters')
        if not request_count:
            issues.append('missing_requests')
    return {'task': name, 'trial': raw.get('trial_name'), 'result': str(path),
            'observed_reward': reward, 'reward': reward if not issues else None,
            'exception': exception, 'issues': sorted(set(issues)), 'requests': request_count}


def panel(job, archives=None, current=True):
    candidates = list(job.glob('*/result.json'))
    if archives and archives.exists():
        candidates += list(archives.glob(f'{job.name}/*/execution-*/result.json'))
    grouped = defaultdict(list)
    for path in candidates:
        row = trial(path, current)
        grouped[row['trial']].append((path.stat().st_mtime, row))
    rows = []
    for values in grouped.values():
        ordered = sorted(values, key=lambda value: (value[0], value[1]['result']))
        accepted = [row for _, row in ordered if row['reward'] is not None]
        rows.append(accepted[0] if accepted else ordered[-1][1])
    return rows


def metrics(rows, tasks, attempts):
    known = [r['reward'] for r in rows if r['task'] in tasks and r['reward'] is not None]
    expected = len(tasks) * attempts
    success = sum(known)
    missing = expected - len(known)
    solved = sum(any(r['task'] == t and r['reward'] == 1 for r in rows) for t in tasks)
    uncertain = sum(not any(r['task'] == t and r['reward'] == 1 for r in rows)
                    and sum(r['task'] == t and r['reward'] is not None for r in rows) < attempts for t in tasks)
    return {'expected': expected, 'valid': len(known), 'success': success,
            'failure': len(known) - success, 'missing': missing,
            'success_rate_valid': success / len(known) if known else None,
            'pass_at_1_bounds': [success / expected, (success + missing) / expected],
            'pass_at_3_bounds': [solved / len(tasks), (solved + uncertain) / len(tasks)]}


def audit(job, baseline, tasks, archives, out):
    rows = panel(job, archives)
    counts = Counter(row['task'] for row in rows)
    shape = len(rows) == len(tasks) * 3 and set(counts) == set(tasks) and all(v == 3 for v in counts.values())
    accepted = shape and all(row['reward'] is not None for row in rows)
    old = panel(baseline, current=False)
    report = {'E10': metrics(rows, tasks, 3), '1001': metrics(old, tasks, 3),
              'per_task': {t: {name: [r['reward'] for r in rs if r['task'] == t]
                              for name, rs in [('E10', rows), ('1001', old)]} for t in tasks},
              'baseline_comparable': False, 'closed_set': True,
              'provider_262144_context_proven': False,
              'environment_differences': ['process-local HTTP/1.1', 'Tencent DNS/mirrors',
                  'ready verifier caches', 'verifier stdin closed'],
              'missing_policy': 'Missing remains null; intervals vary unknown scores from zero to one.'}
    report['usage'] = [{'result': row['result'],
        'agent_result': read(Path(row['result'])).get('agent_result')} for row in rows]
    report['cache_evidence'] = [{'trial': row['trial'],
        'pipeline': read(Path(row['result']).parent / 'pipeline_cache.json'),
        'task_verifier': read(Path(row['result']).parent / 'task_verifier_cache.json'),
        'downloads': read(Path(row['result']).parent / 'pipeline_cache_downloads.json')}
        for row in rows]
    result = {'complete': accepted, 'unique_results': len({r['result'] for r in rows}),
              'trial_shape_valid': shape, 'rows': rows,
              'exceptions': dict(Counter(r['exception'] for r in rows if r['exception']))}
    for name, value in [('execution_audit.json', result), ('report.json', report)]:
        (out / name).write_text(json.dumps(value, indent=2) + '\n')
    return result, report
