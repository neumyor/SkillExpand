"""HKUDS TB ACP pipeline with explicit Tencent/Terminus execution adaptation."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
import re
from pathlib import Path
import signal
import sys
import tomllib
from hkuds_e9_recovery import (relocated, frozen_contract, install_adapter_paths,
    mark_task, allow_failed_record, create_amendment, MISSING_BASELINE)

UPSTREAM_REVISION = 'ed42b147411bde72620a9a61bc956693413b0607'
FILES = {'/app/CLAUDE.md': 'CLAUDE.md',
         '/app/.claude/skills/solve-task.md': 'solve-task.md',
         '/app/memory/feedback.md': 'feedback.md'}


def upstream_prompts(source):
    provenance = json.loads((source / 'SOURCE.json').read_text())
    if provenance['revision'] != UPSTREAM_REVISION:
        raise ValueError('Unexpected official source revision')
    spec = importlib.util.spec_from_file_location('official_brew', source / 'scripts/run_brew.py')
    brew = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brew)
    result = {'brew': brew.build_brew_prompt}
    for key, name, variable in [('curator', 'run_curator.py', 'curator_prompt'),
                                 ('revision', 'run_ralph.py', 'revise_prompt')]:
        tree = ast.parse((source / 'scripts' / name).read_text())
        value = next(node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets))
        result[key] = compile(ast.Expression(value), str(source / 'scripts' / name), 'eval')
    return result


def trace_text(path, steps, width):
    trajectory = json.loads(Path(path).read_text())
    return ''.join((message[:width] + '...\n' if len(message) > width else message + '\n')
                   for step in trajectory.get('steps', [])[-steps:]
                   if (message := step.get('message', '')))


def complete_stream(transport, payload, diagnostics=None):
    from skillexpand.runtime.llm_relay import iter_sse_events
    content, reasoning, calls, usage, finish = '', '', {}, None, None
    def chunks():
        for chunk in transport.stream(payload):
            if diagnostics is not None:
                diagnostics['raw'].write(chunk if isinstance(chunk, bytes) else chunk.encode())
                diagnostics['raw'].flush()
            yield chunk
    for event in iter_sse_events(chunks()):
        if event.get('usage'):
            usage = event['usage']
        if diagnostics is not None:
            diagnostics['usage'] = usage
        for choice in event.get('choices') or []:
            delta = choice.get('delta') or {}
            content += delta.get('content') or ''
            reasoning += delta.get('reasoning_content') or ''
            finish = choice.get('finish_reason') or finish
            if diagnostics is not None:
                diagnostics.update(content=content, reasoning_content=reasoning,
                                   tool_calls=list(calls.values()), finish_reason=finish)
            for call in delta.get('tool_calls') or []:
                row = calls.setdefault(call['index'], {'id': '', 'type': 'function',
                    'function': {'name': '', 'arguments': ''}})
                row['id'] = call.get('id') or row['id']
                for field in ('name', 'arguments'):
                    row['function'][field] += (call.get('function') or {}).get(field) or ''
            if diagnostics is not None:
                diagnostics['tool_calls'] = list(calls.values())
    message = {'role': 'assistant', 'content': content, 'tool_calls': list(calls.values())}
    if finish not in ('stop', 'tool_calls'):
        raise ValueError('Teacher stream did not complete within its output budget')
    if reasoning:
        message['reasoning_content'] = reasoning
    return message, usage, finish


class ACPTeacher:
    def __init__(self, transport, model, save):
        self.transport, self.model, self.save = transport, model, save

    def run(self, root, prompt):
        settings = {'model': self.model, 'max_tokens': 65536, 'top_p': 0.95,
                    'top_k': 20, 'enable_thinking': True, 'reasoning_effort': 'max',
                    'chat_template_kwargs': {'preserve_thinking': True},
                    'context_limit': 1048576, 'tool_turn_limit': 8}
        settings_path = root / 'settings.json'
        if settings_path.exists() and json.loads(settings_path.read_text()) != settings:
            raise ValueError('Cached ACP model or request settings differ')
        root.mkdir(parents=True, exist_ok=True)
        self.save(settings_path, settings)
        final = root / 'acp.json'
        if final.exists():
            if (root / 'prompt.txt').read_text() != prompt:
                raise ValueError('Cached ACP prompt differs')
            return json.loads(final.read_text())
        root.mkdir(parents=True, exist_ok=True)
        prompt_path = root / 'prompt.txt'
        if prompt_path.exists() and prompt_path.read_text() != prompt:
            raise ValueError('Teacher prompt changed')
        prompt_path.write_text(prompt)
        records = sorted(root.glob('http-*.json'))
        if len(records) > 8 or [p.name for p in records] != [f'http-{i:02d}.json' for i in range(len(records))]:
            raise ValueError('Teacher request slots exceed or differ from the original eight-turn budget')
        messages = [{'role': 'user', 'content': prompt}]
        files = {}
        tools = [{'type': 'function', 'function': {'name': 'Write',
            'description': 'Write a text file to an absolute path.',
            'parameters': {'type': 'object', 'properties': {
                'file_path': {'type': 'string'}, 'content': {'type': 'string'}},
                'required': ['file_path', 'content']}}}]
        for turn in range(8):
            path = root / f'http-{turn:02d}.json'
            body = {'model': self.model, 'messages': messages, 'tools': tools,
                    'stream': True, 'max_tokens': 65536, 'top_p': 0.95, 'top_k': 20,
                    'enable_thinking': True, 'reasoning_effort': 'max',
                    'chat_template_kwargs': {'preserve_thinking': True}}
            if path.exists():
                evidence = json.loads(path.read_text())
                if evidence['request'] != body:
                    raise ValueError('Teacher attempt changed or incomplete; no automatic resampling')
                if evidence['status'] != 'completed':
                    recovery = path.with_name('recovery-' + path.name)
                    if (not recovery.exists()
                            or json.loads(recovery.read_text()).get('request') != body
                            or json.loads(recovery.read_text()).get('spent_tool_turn') != turn
                            or (evidence['status'] not in ('pending', 'transport_failed')
                                and not allow_failed_record(root, path, body, turn))
                            or (evidence.get('error_type') not in (None, 'RelayError')
                                and not allow_failed_record(root, path, body, turn))):
                        raise ValueError('Teacher attempt changed or incomplete; no automatic resampling')
                    # Failed transport still consumes its original tool-turn slot.
                    continue
                message = evidence['response']
            else:
                self.save(path, {'status': 'pending', 'request': body})
                diagnostics = {'usage': None, 'finish_reason': None}
                try:
                    with path.with_suffix('.sse').open('xb') as raw:
                        diagnostics['raw'] = raw
                        message, usage, finish = complete_stream(self.transport, body, diagnostics)
                except Exception as exc:
                    self.save(path, {'status': 'transport_failed' if type(exc).__name__ == 'RelayError' else 'teacher_failed', 'request': body,
                                    'error_type': type(exc).__name__,
                                    'error': re.sub(r'(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]+)', '[REDACTED]', str(exc))[:8000],
                                    'usage': diagnostics.get('usage'), 'outcome_unknown': True,
                                    'diagnostics': {k: v for k, v in diagnostics.items() if k != 'raw'}})
                    raise
                self.save(path, {'status': 'completed', 'request': body,
                                'response': message, 'usage': usage, 'finish_reason': finish})
            messages.append(message)
            for call in message['tool_calls']:
                if call['function']['name'] != 'Write':
                    raise ValueError('Unsupported teacher tool')
                arguments = json.loads(call['function']['arguments'])
                target = arguments['file_path']
                if not isinstance(arguments['content'], str):
                    raise ValueError('Teacher Write content must be a string')
                if target in FILES:
                    files[FILES[target]] = arguments['content']
                messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                 'content': 'File written successfully.'})
            if not message['tool_calls']:
                break
        if set(files) != set(FILES.values()) or any(len(v) < 100 for v in files.values()):
            raise ValueError('Teacher did not write the complete three-file ACP')
        self.save(final, files)
        for name, value in files.items():
            (root / name).write_text(value)
        return files


def true_reward(trial):
    if (trial.get('terminal_failure') or trial.get('infra')
            or trial.get('reward') not in (0, 1)
            or trial.get('verifier_evidence', {}).get('status') != 'tests_executed'):
        return None
    return trial['reward']


def trajectory_path(trial):
    return relocated(trial['trajectory_path'])


def complete_audit(report):
    return (not report['evo_missing'] and not report['serve']['missing']
            and not report['vanilla']['missing']
            and 'missing' not in report['ralph_status'].values())


def unstarted_brew_tasks(root, eligible):
    return [task for task in eligible
            if not (root / 'brew' / task / 'acp.json').exists()
            and not any((root / 'brew' / task).glob('http-*.json'))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--eval-config', type=Path, required=True)
    parser.add_argument('--student-workers', type=int, default=64)
    parser.add_argument('--teacher-workers', type=int, default=16)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--brew-unstarted-only', action='store_true')
    parser.add_argument('--recovery-preflight', action='store_true')
    parser.add_argument('--bounded-recovery', action='store_true')
    parser.add_argument('--recovery-task')
    args = parser.parse_args()
    from tbench_eval.agentbrew.adapter import RolloutAdapter
    from tbench_eval.agentbrew.adapter import read_eval_contract
    from tbench_eval.agentbrew.storage import digest, read, run_lock, save, text_hash
    from skillexpand.runtime.llm_relay import TencentSandboxTransport
    # E9 teacher streams share envd connections; avoid the observed HTTP/2 TLS failure.
    from functools import partial
    import e2b.sandbox_async.main as e2b_async_main
    from e2b.api.client_async import get_envd_transport
    e2b_async_main.get_transport = partial(get_envd_transport, http2=False)
    adapter = RolloutAdapter(args.eval_config, 2, rollout_backend='tb21-tencent',
        harness_path='/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/agentbrew-tb21-evo89-test89-20261012/sources/tb21-tencent-harness/python',
        task_failure_policy='terminal')
    prompts = upstream_prompts(args.source)
    task_root = Path(adapter.config.dataset.local_path)
    tasks = sorted(p.name for p in task_root.iterdir() if (p / 'instruction.md').exists())
    if args.smoke:
        tasks = ['fix-git']
    elif len(tasks) != 89:
        raise ValueError('Expected the full 89-task snapshot')
    contract = {'source': 'https://github.com/HKUDS/UpSkill', 'revision': UPSTREAM_REVISION,
        'protocol': 'same-task-set evo89/test89; one rollout per formal group',
        'phases': ['baseline', 'brew', 'curate', 'ralph', 'serve', 'vanilla'],
        'tasks': tasks, 'student': 'qwen3.6-flash-distill',
        'teacher': 'deepseek-v4-flash-0731', 'max_ralph_rounds': 3,
        'retrieval': 'category ACP; no BGE and no quality threshold',
        'adaptations': ['TB2.1 user task set and model settings',
            'Tencent Terminus student instead of Claude Code',
            'OpenAI Write tool teacher instead of Claude Code teacher',
            'CLAUDE.md injected as immutable student system guidance',
            'real verifier rewards only; unknown excluded from PASS/FAIL',
            'durable attempts and ownership; no deletion of prior jobs'],
        'curation_timing': 'before Ralph, as official TB scripts; no implicit recuration',
        'teacher_tool_turn_limit': 8,
        'eval_config': read_eval_contract(args.eval_config),
        'teacher_settings': {'max_input_tokens': 1048576, 'max_output_tokens': 65536,
            'reasoning_effort': 'max', 'enable_thinking': True, 'preserve_thinking': True,
            'top_p': 0.95, 'top_k': 20},
        'smoke': args.smoke}
    with run_lock(args.root / 'owner'):
        args.root.mkdir(parents=True, exist_ok=True)
        path = args.root / 'contract.json'
        if path.exists():
            contract = frozen_contract(contract, read(path))
            install_adapter_paths(adapter, contract)
        else:
            save(path, contract)
        if args.recovery_preflight or args.bounded_recovery:
            from hkuds_e9_recovery import preflight
            preflight(args.root, adapter, contract, prompts, task_root, save)
            if args.recovery_preflight:
                return
        def stop(signum, frame):
            adapter.cancel_all()
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        transport = TencentSandboxTransport(template=os.environ['TBENCH_E2B_RELAY_TEMPLATE'],
            api_key=os.environ['TB21_METHOD_API_KEY'], api_base='https://llm-center.modelbest.co/v1',
            metadata=json.loads(os.environ.get('TBENCH_E2B_RELAY_METADATA_JSON', '{}')),
            request_timeout=1800)
        teacher = ACPTeacher(transport, contract['teacher'], save)
        def parallel(items, workers, operation):
            if workers == 1:
                return {item: operation(item) for item in items}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                return dict(zip(items, pool.map(operation, items)))
        def phase(name):
            save(args.root / 'status.json', {'phase': name, 'status': 'running', 'pid': os.getpid(),
                'workers': args.student_workers, 'teacher_workers': args.teacher_workers})
        def rollout(task, stage, guidance=''):
            instruction = (task_root / task / 'instruction.md').read_text()
            from harbor.models.task.task import Task
            request = {'task': task, 'phase': stage, 'instruction_hash': text_hash(instruction),
                'task_checksum': Task(task_root / task).checksum, 'contract_hash': digest(contract),
                'retrieval': {'selected': [], 'rendered_system': guidance,
                              'retrieval_config': {'max_memory_tokens': None}}}
            return adapter.run(args.root / stage / task, request)
        try:
            phase('baseline')
            baseline = parallel(tasks, args.student_workers, lambda t: rollout(t, 'baseline'))
            eligible = [t for t in tasks if true_reward(baseline[t]) is not None]
            categories = {t: tomllib.loads((task_root / t / 'task.toml').read_text())['metadata']['category'] for t in tasks}
            def draft(task):
                trial = baseline[task]
                trajectory = trajectory_path(trial)
                prompt = prompts['brew']((task_root / task / 'instruction.md').read_text(),
                    trace_text(trajectory, 15, 250), 'PASS' if true_reward(trial) else 'FAIL')
                prompt = prompt.replace('(deepseek-v4-pro)', '(deepseek-v4-flash-0731)').replace('(deepseek-v4-flash)', '(qwen3.6-flash-distill)')
                return teacher.run(args.root / 'brew' / task, prompt)
            phase('brew')
            if args.bounded_recovery:
                amendment = create_amendment(args.root, save)
                selected = amendment['teacher_tasks']
                if args.recovery_task:
                    if args.recovery_task not in selected:
                        raise ValueError('Task outside authorized recovery')
                    selected = [args.recovery_task]
                for task in selected:
                    if (args.root / 'brew' / task / 'acp.json').exists():
                        draft(task)
                        continue
                    mark_task(args.root, task, save)
                    phase('brew-recovery-' + task)
                    draft(task)
                if args.recovery_task:
                    save(args.root / 'status.json', {'phase': 'recovery_task_complete',
                        'task': args.recovery_task, 'status': 'needs_attention', 'workers': 0})
                    return
                from hkuds_e9_recovery import recover_baseline
                for task in MISSING_BASELINE:
                    request = read(args.root / 'baseline' / task / 'request.json')
                    phase('baseline-recovery-' + task)
                    baseline[task] = recover_baseline(args.root, task, adapter, request, save)
                eligible = [t for t in tasks if true_reward(baseline[t]) is not None]
                if len(eligible) != 89:
                    raise ValueError('Recovery requires all 89 real baseline scores')
                for task in MISSING_BASELINE:
                    phase('brew-recovered-baseline-' + task)
                    draft(task)
            if args.brew_unstarted_only:
                selected = unstarted_brew_tasks(args.root, eligible)
                save(args.root / 'brew_unstarted_scope.json', {
                    'selected': selected, 'eligible': eligible,
                    'existing_request_units_excluded': [t for t in eligible if t not in selected],
                    'budget_resets': 0, 'teacher_turn_limit': 8,
                    'reason': 'Advance untouched units while retaining failed original requests'})
                errors = {}
                def bounded_draft(task):
                    try:
                        return draft(task)
                    except Exception as exc:
                        errors[task] = {'error_type': type(exc).__name__}
                        return None
                parallel(selected, args.teacher_workers, bounded_draft)
                missing = [t for t in eligible if not (args.root / 'brew' / t / 'acp.json').exists()]
                save(args.root / 'brew_unstarted_result.json', {
                    'selected': selected, 'errors': errors, 'missing': missing,
                    'complete_acps': len(eligible) - len(missing), 'budget_resets': 0,
                    'curate_started': False})
                save(args.root / 'status.json', {'status': 'needs_attention',
                    'phase': 'brew_incomplete' if missing else 'brew_scope_finished',
                    'workers': 0, 'missing_acps': missing})
                return
            brew_errors = {}
            def draft_retaining_errors(task):
                try:
                    return draft(task)
                except Exception as exc:
                    brew_errors[task] = {'error_type': type(exc).__name__,
                                         'error': str(exc)[:1000]}
                    return None
            acps = ({task: draft(task) for task in eligible} if args.bounded_recovery else
                    parallel(eligible, args.teacher_workers, draft_retaining_errors))
            missing_acps = [task for task, acp in acps.items() if acp is None]
            save(args.root / 'brew_status.json', {'complete_acps': len(eligible) - len(missing_acps),
                'missing': missing_acps, 'errors': brew_errors, 'budget_resets': 0,
                'extra_write_policy': 'preserve calls; extract only ACP files'})
            if missing_acps:
                save(args.root / 'status.json', {'status': 'needs_attention',
                    'phase': 'brew_incomplete', 'workers': 0, 'missing_acps': missing_acps})
                return
            phase('curate')
            cats = sorted({categories[t] for t in eligible})
            def curate(cat):
                own = sorted(t for t in eligible if categories[t] == cat)
                env = {'cat': cat}
                for key, filename in [('claude_parts','CLAUDE.md'),('skill_parts','solve-task.md'),('feedback_parts','feedback.md')]:
                    env[key] = [f'### From: {t}\n{acps[t][filename]}' for t in own]
                return teacher.run(args.root / 'curated' / cat, eval(prompts['curator'], {}, env))
            curated = parallel(cats, args.teacher_workers, curate)
            ralph_status = {t: 'pending' for t in eligible}
            for round_number in range(1, 4):
                active = [t for t in eligible if ralph_status[t] not in ('validated', 'missing')]
                phase(f'ralph-{round_number}')
                results = parallel(active, args.student_workers,
                    lambda t: rollout(t, f'ralph-{round_number}', acps[t]['CLAUDE.md']))
                failures = []
                for t in active:
                    reward = true_reward(results[t])
                    ralph_status[t] = 'validated' if reward == 1 else ('missing' if reward is None else f'failed_after_{round_number}')
                    if reward == 0:
                        failures.append(t)
                save(args.root / 'ralph_status.json', ralph_status)
                if round_number < 3:
                    def revise(t):
                        trajectory = trajectory_path(results[t])
                        env = {'inst': (task_root / t / 'instruction.md').read_text(),
                               'current_claude': acps[t]['CLAUDE.md'][:2000],
                               'failure_text': trace_text(trajectory, 10, 200)}
                        return teacher.run(args.root / f'revision-{round_number+1}' / t,
                                           eval(prompts['revision'], {}, env))
                    revised = parallel(failures, args.teacher_workers, revise)
                    for t, value in revised.items():
                        # Official run_ralph persists only the revised CLAUDE.md.
                        acps[t] = {**acps[t], 'CLAUDE.md': value['CLAUDE.md']}
            save(args.root / 'frozen_acp.json', {'categories': curated,
                'source': 'official pre-Ralph category curation', 'teacher_calls_during_test': 0})
            for cat, files in curated.items():
                target = args.root / 'frozen' / cat
                target.mkdir(parents=True, exist_ok=True)
                for filename, content in files.items():
                    (target / filename).write_text(content)
            phase('serve')
            teacher_before_test = sorted(str(p) for p in args.root.glob('**/http-*.json'))
            serve = parallel(tasks, args.student_workers,
                lambda t: rollout(t, 'serve', curated.get(categories[t], {}).get('CLAUDE.md', '')))
            phase('vanilla')
            vanilla = parallel(tasks, args.student_workers, lambda t: rollout(t, 'vanilla'))
            if teacher_before_test != sorted(str(p) for p in args.root.glob('**/http-*.json')):
                raise ValueError('Teacher requests changed during test')
            def metrics(rows):
                rewards = [true_reward(rows[t]) for t in tasks]
                successes = rewards.count(1)
                missing = rewards.count(None)
                return {'successes': successes, 'failures': rewards.count(0), 'missing': missing,
                        'success_rate': successes / len(tasks) if not missing else None,
                        'success_rate_bounds': [successes / len(tasks), (successes + missing) / len(tasks)]}
            report = {'closed_set': True, 'serve': metrics(serve), 'vanilla': metrics(vanilla),
                'baseline': metrics(baseline), 'evo_missing': sorted(set(tasks)-set(eligible)),
                'ralph_status': ralph_status, 'category_count': len(curated),
                'teacher_calls_during_test': 0,
                'per_task': {t: {'serve': true_reward(serve[t]), 'vanilla': true_reward(vanilla[t])} for t in tasks}}
            report['usage'] = {'physical_rollouts': [], 'teacher_requests': []}
            for path in sorted(args.root.glob('*/*/physical_*/result.json')):
                value = read(path)
                report['usage']['physical_rollouts'].append({'path': str(path),
                    'usage': value.get('usage'), 'infra': value.get('infra'),
                    'exception': value.get('exception')})
            for path in sorted(args.root.glob('**/http-*.json')):
                value = read(path)
                report['usage']['teacher_requests'].append({'path': str(path),
                    'status': value['status'], 'usage': value.get('usage')})
            report['ralph_missing'] = [t for t, status in ralph_status.items() if status == 'missing']
            save(args.root / 'report.json', report)
            save(args.root / 'audit.json', {'complete': complete_audit(report),
                'unique_baseline_tasks': len(baseline), 'unique_serve_tasks': len(serve),
                'unique_vanilla_tasks': len(vanilla), 'teacher_calls_during_test': 0,
                'source_revision': UPSTREAM_REVISION, 'adapted_execution': True})
            save(args.root / 'status.json', {'status': 'complete' if complete_audit(report) else 'audited_incomplete',
                'workers': 0, 'phase': 'finished'})
        except Exception as exc:
            save(args.root / 'status.json', {'status': 'needs_attention', 'phase': 'failed',
                'error_type': type(exc).__name__, 'error': str(exc)[:1000], 'workers': 0})
            raise
        finally:
            adapter.cancel_all()
            transport.close()


if __name__ == '__main__':
    main()
