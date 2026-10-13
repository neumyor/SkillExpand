"""Audit retained E8 evidence and compare task-level outcomes with E2."""
import argparse
from collections import Counter, defaultdict
import json
import re
from pathlib import Path


def panel(root, e8=False):
    rows = []
    for path in sorted(root.glob('*/result.json')):
        raw = json.loads(path.read_text())
        trial = path.parent
        task = raw['task_name']
        issues = []
        reward = ((raw.get('verifier_result') or {}).get('rewards') or {}).get('reward')
        exception = (raw.get('exception_info') or {}).get('exception_type')
        if raw['trial_name'] != trial.name or not trial.name.startswith(task + '__'):
            issues.append('identity')
        agent = raw['config']['agent']
        if agent['model_name'] != 'openai/deepseek-v4-flash-0731':
            issues.append('model')
        trajectory = trial / 'agent/trajectory.json'
        if not trajectory.exists() or not json.loads(trajectory.read_text()):
            issues.append('trajectory')
        reward_file = trial / 'verifier/reward.txt'
        stdout = trial / 'verifier/test-stdout.txt'
        if reward not in (0, 1):
            issues.append('missing_reward')
        elif exception == 'VerifierTimeoutError':
            issues.append('verifier_timeout')
        elif not reward_file.exists() or float(reward_file.read_text().strip()) != reward:
            issues.append('verifier_reward_file')
        log = stdout.read_text() if stdout.exists() else ''
        if 'test session starts' not in log or not re.search(r'\b[1-9]\d* (?:passed|failed)\b', log):
            issues.append('verifier_execution_evidence')
        requests = 0
        if e8:
            kwargs = agent['kwargs']
            if kwargs['model_info']['max_input_tokens'] != 1048576:
                issues.append('context_config')
            if raw['config'].get('mounts'):
                issues.append('unexpected_mounts')
            request_file = trial / 'agent/raw_model_requests.jsonl'
            if request_file.exists():
                # File iteration splits on LF only; Unicode separators can occur in JSON strings.
                with request_file.open() as source:
                    for line in source:
                        if not line.strip():
                            continue
                        request = json.loads(line)
                        requests += 1
                        if (request.get('model') != 'deepseek-v4-flash-0731'
                                or request.get('reasoning_effort') != 'max'
                                or request.get('max_tokens') != 65536
                                or request.get('top_p') != 0.95
                                or request.get('top_k') != 20
                                or request.get('enable_thinking') is not True
                                or request.get('chat_template_kwargs', {}).get('preserve_thinking') is not True):
                            issues.append('http_parameters')
                            break
            if not requests:
                issues.append('missing_http_requests')
        rows.append(dict(task=task, trial=trial.name, result=str(path),
                         observed_reward=reward, reward=reward if not issues else None,
                         exception=exception, issues=sorted(set(issues)), requests=requests))
    return rows


def metrics(rows):
    tasks = defaultdict(list)
    for row in rows:
        tasks[row['task']].append(row['reward'])
    known = [row['reward'] for row in rows if row['reward'] in (0, 1)]
    successes = sum(known)
    missing = len(rows) - len(known)
    solved = sum(1 in rewards for rewards in tasks.values())
    uncertain = sum(1 not in rewards and None in rewards for rewards in tasks.values())
    return dict(trials=len(rows), tasks=len(tasks), valid_scores=len(known),
                successes=successes, failures=len(known) - successes, missing=missing,
                success_rate_valid=successes / len(known) if known else None,
                pass_at_1_bounds=[successes / len(rows), (successes + missing) / len(rows)],
                pass_at_3_bounds=[solved / len(tasks), (solved + uncertain) / len(tasks)],
                solved_tasks=solved, uncertain_tasks=uncertain,
                complete_three_trial_tasks=sum(len(v) == 3 and None not in v for v in tasks.values()))


def merge_recovery(original, recovery):
    by_trial = {row['trial']: row for row in original}
    for row in recovery:
        prior = by_trial.get(row['trial'])
        if prior is None or prior['task'] != row['task']:
            raise ValueError('Recovery identity absent from original panel')
        if prior['reward'] is not None:
            raise ValueError('Recovery would replace an accepted score')
        if row['reward'] is not None:
            intent_path = Path(row['result']).parent / 'recovery_intent.json'
            intent = json.loads(intent_path.read_text())
            if (Path(intent['original_result']).resolve() != Path(prior['result']).resolve()
                    or intent.get('previous_executions') != 1
                    or intent.get('execution_limit') != 2
                    or intent.get('remaining_after_reservation') != 0):
                raise ValueError('Recovery has no valid cumulative budget reservation')
            by_trial[row['trial']] = dict(row, historical_result=prior['result'],
                                           recovery=True)
    return list(by_trial.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--recovery', type=Path)
    args = parser.parse_args()
    current, baseline = panel(args.job, True), panel(args.baseline)
    if args.recovery:
        current = merge_recovery(current, panel(args.recovery, True))
    by_task = defaultdict(dict)
    for name, rows in [('E8', current), ('E2', baseline)]:
        for task in sorted({row['task'] for row in rows}):
            rewards = [r['reward'] for r in rows if r['task'] == task]
            by_task[task][name] = dict(rewards=rewards, successes=rewards.count(1),
                                      missing=rewards.count(None), solved=1 in rewards)
    counts = Counter(row['task'] for row in current)
    for pair in by_task.values():
        pair['complete_pair'] = (all(name in pair for name in ['E8', 'E2'])
                                 and all(len(pair[name]['rewards']) == 3 and not pair[name]['missing']
                                         for name in ['E8', 'E2']))
        pair['success_count_delta'] = (pair['E8']['successes'] - pair['E2']['successes']
                                       if pair['complete_pair'] else None)
    shape_ok = len(current) == 267 and len(counts) == 89 and all(v == 3 for v in counts.values())
    audit = dict(complete=shape_ok and all(not r['issues'] for r in current),
                 trial_shape_valid=shape_ok, unique_results=len({r['result'] for r in current}),
                 valid_scores=sum(r['reward'] is not None for r in current),
                 rejected=[r for r in current if r['issues']], rows=current,
                 exceptions=dict(Counter(r['exception'] for r in current if r['exception'])))
    report = dict(E8=metrics(current), E2=metrics(baseline), per_task=by_task,
                  baseline_comparable=False, closed_set=True,
                  missing_policy='Unknown stays null. Bounds vary missing rewards from 0 to 1.',
                  provider_full_context_or_effort_proven=False,
                  environment_differences='Historical E8 uses pipeline v6 only for torch-pipeline-parallelism; bare Snapshot setup did not apply the Skill package mirrors or generic uv cache. Later infrastructure repairs do not change these historical trials.')
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in [('execution_audit.json', audit), ('report.json', report)]:
        (args.out / name).write_text(json.dumps(data, indent=2) + '\n')
    status_path = args.out / 'status.json'
    if status_path.exists():
        status = json.loads(status_path.read_text())
        if status.get('phase') == 'full_finished':
            status.update(status='accepted' if audit['complete'] else 'audited_incomplete',
                          coverage=f"{audit['valid_scores']}/267", workers=0,
                          unresolved=len(audit['rejected']))
            status_path.write_text(json.dumps(status, indent=2) + '\n')
    print(json.dumps(dict(audit_complete=audit['complete'], E8=report['E8'], E2=report['E2'],
                          rejected_issues=dict(Counter(i for r in audit['rejected'] for i in r['issues'])))))


if __name__ == '__main__':
    main()
