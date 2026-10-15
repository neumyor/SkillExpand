"""Scoped E9 relocation and user-authorized recovery, preserving raw artifacts."""
import copy
import json
from pathlib import Path

REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
BASE = REPO / 'runs/agentbrew-tb21-evo89-test89-20261012'
OLD = Path('/home/xinhaidong/agentbrew-tb21-evo89-test89-20261012')
MISSING_BASELINE = ('reshard-c4-data', 'feal-differential-cryptanalysis')
PATH_FIELDS = {'raw_result_path', 'trajectory_path', 'source_physical_result_path',
               'physical_dir', 'log_path'}
TEN_TURN_AMENDMENT = 'recovery_ten_turn_amendment_20261015.json'
LENGTH_CONTINUATION = 'recovery_after_length_amendment_20261015.json'
LENGTH_REWARD_POLICY = {
    'authorization': 'user_finish_reason_length_reward_zero_20261015',
    'scope': 'E9 teacher requests',
    'finish_reason': 'length', 'reward': 0,
    'basis': 'finish_reason_length',
    'student_verifier_reward': False,
    'grants_acp_completion': False,
    'grants_retry': False,
}


def teacher_request_score(record):
    finish = record.get('finish_reason') or (record.get('diagnostics') or {}).get('finish_reason')
    return {'finish_reason': finish, 'reward': 0 if finish == 'length' else None,
            'reward_basis': LENGTH_REWARD_POLICY['basis'] if finish == 'length' else None}


def active_amendment(root):
    original_path = root / 'recovery_amendment_20261015.json'
    original = json.loads(original_path.read_text())
    path = root / TEN_TURN_AMENDMENT
    if not path.exists():
        return original_path, original
    value = json.loads(path.read_text())
    if (value.get('authorization') != 'user_approved_ten_turn_resume_20261015'
            or value.get('teacher_turn_limit') != 10
            or value.get('previous_teacher_turn_limit') != 8
            or value.get('budget_resets') != 0
            or value.get('teacher_tasks') != original['teacher_tasks']
            or value.get('baseline_extra_dispatches') != original['baseline_extra_dispatches']
            or value.get('previous_amendment') != original
            or value.get('stop_on_first_failure') is not True):
        raise ValueError('Ten-turn recovery amendment differs')
    continuation = root / LENGTH_CONTINUATION
    if continuation.exists():
        resumed = json.loads(continuation.read_text())
        if (resumed.get('authorization') != 'user_continue_e9_after_length_20261015'
                or resumed.get('previous_amendment') != value
                or resumed.get('teacher_turn_limit') != 10
                or resumed.get('budget_resets') != 0
                or resumed.get('teacher_tasks') != value['teacher_tasks']
                or resumed.get('baseline_extra_dispatches') != value['baseline_extra_dispatches']
                or resumed.get('stop_on_first_failure') is not True):
            raise ValueError('Post-length continuation amendment differs')
        return continuation, resumed
    return path, value


def create_length_continuation(root, save):
    if (root / LENGTH_CONTINUATION).exists():
        return active_amendment(root)[1]
    previous_path, previous = active_amendment(root)
    if previous_path.name != TEN_TURN_AMENDMENT:
        raise ValueError('Post-length continuation requires the ten-turn amendment')
    path = root / 'brew/regex-chess/http-02.json'
    if teacher_request_score(json.loads(path.read_text()))['reward_basis'] != 'finish_reason_length':
        raise ValueError('Expected the inspected regex-chess length failure')
    failures = {}
    for task in previous['teacher_tasks']:
        records = sorted((root / 'brew' / task).glob('http-*.json'))
        if len(records) > 10 or [p.name for p in records] != [f'http-{i:02d}.json' for i in range(len(records))]:
            raise ValueError('Teacher history exceeds the cumulative ten-turn budget')
        failures[task] = {p.name: json.loads(p.read_text()) for p in records
                         if json.loads(p.read_text())['status'] != 'completed'}
    value = {**previous, 'authorization': 'user_continue_e9_after_length_20261015',
             'previous_amendment': previous, 'teacher_failures': failures,
             'reason': 'User explicitly requested E9 continue after length was scored zero'}
    save(root / LENGTH_CONTINUATION, value)
    return active_amendment(root)[1]


def teacher_turn_limit(root):
    if (root / TEN_TURN_AMENDMENT).exists():
        return active_amendment(root)[1]['teacher_turn_limit']
    return 8


def create_ten_turn_amendment(root, save):
    original = create_amendment(root, save)
    path = root / TEN_TURN_AMENDMENT
    if path.exists():
        return active_amendment(root)[1]
    failures = {}
    for task in original['teacher_tasks']:
        records = sorted((root / 'brew' / task).glob('http-*.json'))
        if len(records) > 8 or [p.name for p in records] != [f'http-{i:02d}.json' for i in range(len(records))]:
            raise ValueError('Original teacher history exceeds the prior budget')
        failures[task] = {p.name: json.loads(p.read_text()) for p in records
                         if json.loads(p.read_text())['status'] != 'completed'}
    value = {**original, 'authorization': 'user_approved_ten_turn_resume_20261015',
        'teacher_turn_limit': 10, 'previous_teacher_turn_limit': 8,
        'previous_amendment': original, 'teacher_failures': failures,
        'scope': 'All E9 teacher units; cumulative physical request slots, no hidden retries',
        'reason': 'User requested ten attempts and rerun after regex-chess provider 500'}
    save(path, value)
    return active_amendment(root)[1]


def relocated(path):
    path = Path(path)
    try:
        relative = path.relative_to('/home/xinhaidong')
    except ValueError:
        return path
    if relative.parts and relative.parts[0].startswith('agentbrew'):
        return REPO / 'runs' / relative
    return path


def evidence_paths(value):
    if isinstance(value, list):
        return [evidence_paths(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: str(relocated(item)) if key in PATH_FIELDS and isinstance(item, str)
            else (item if key in {'request', 'eval_config'} else evidence_paths(item))
            for key, item in value.items()}


def frozen_contract(current, saved):
    candidate = copy.deepcopy(current)
    old_source = saved['eval_config']['source_path']
    new_source = candidate['eval_config']['source_path']
    if relocated(old_source) != Path(new_source):
        raise ValueError('E9 config relocation differs')
    candidate['eval_config']['source_path'] = old_source
    if candidate != saved:
        raise ValueError('Official revision contract changed beyond relocation')
    return saved


def install_adapter_paths(adapter, contract):
    """Change only this E9 driver's module reads, never the evidence on disk."""
    import tbench_eval.agentbrew.adapter as module
    import tbench_eval.agentbrew.verifier as verifier
    original_read = module.read
    original_contract = module.read_eval_contract
    original_verifier_read = verifier.read
    original_verifier_evidence = verifier.verifier_evidence

    class EvidencePath(type(Path())):
        def __new__(cls, *parts):
            return super().__new__(cls, relocated(Path(*parts)))

    module.Path = EvidencePath
    module.read = lambda path: evidence_paths(original_read(relocated(path)))
    verifier.read = lambda path: evidence_paths(original_verifier_read(relocated(path)))
    verifier.verifier_evidence = lambda path: evidence_paths(original_verifier_evidence(relocated(path)))
    module.verifier_evidence = verifier.verifier_evidence

    def eval_contract(path):
        actual = original_contract(path)
        expected = contract['eval_config']
        candidate = copy.deepcopy(actual)
        if relocated(expected['source_path']) != Path(candidate['source_path']):
            raise ValueError('Adapter config relocation differs')
        candidate['source_path'] = expected['source_path']
        if candidate != expected:
            raise ValueError('Adapter configuration changed beyond relocation')
        return expected

    module.read_eval_contract = eval_contract
    adapter.legacy_eval_contracts = [contract['eval_config']]


def teacher_order(root):
    rows = []
    for unit in sorted((root / 'brew').iterdir()):
        if not unit.is_dir() or (unit / 'acp.json').exists():
            continue
        records = sorted(unit.glob('http-*.json'))
        if records:
            rows.append((unit.name, 8 - len(records)))
    return [name for name, _ in sorted(rows,
        key=lambda item: (item[0] != 'query-optimize',
                          item[0] == 'break-filter-js-from-html', -item[1], item[0]))]


def allow_failed_record(unit, path, body, turn):
    marker = path.with_name('recovery-' + path.name)
    if not marker.exists():
        return False
    saved = json.loads(marker.read_text())
    if saved.get('request') != body or saved.get('spent_tool_turn') != turn:
        return False
    root = unit.parents[1]
    if not (root / 'recovery_amendment_20261015.json').exists():
        return False
    amendment, value = active_amendment(root)
    record = value.get('teacher_failures', {}).get(unit.name, {}).get(path.name)
    return (value.get('authorization') in {'user_approved_plan_20261015',
                                          'user_approved_ten_turn_resume_20261015',
                                          'user_continue_e9_after_length_20261015'}
            and 0 <= turn < value['teacher_turn_limit']
            and saved.get('amendment') == str(amendment)
            and record == json.loads(path.read_text()))


def create_amendment(root, save):
    path = root / 'recovery_amendment_20261015.json'
    if path.exists():
        value = json.loads(path.read_text())
        if (value.get('authorization') != 'user_approved_plan_20261015'
                or value.get('teacher_turn_limit') != 8
                or value.get('budget_resets') != 0
                or len(set(value.get('teacher_tasks', []))) != 20
                or value.get('teacher_tasks', [None])[0] != 'query-optimize'
                or value.get('baseline_extra_dispatches') != {task: 1 for task in MISSING_BASELINE}):
            raise ValueError('Bounded recovery amendment differs')
        return value
    tasks = teacher_order(root)
    if len(tasks) != 20:
        raise ValueError('Expected exactly the authorized twenty pending teacher units')
    failures = {}
    for task in tasks:
        failures[task] = {}
        records = sorted((root / 'brew' / task).glob('http-*.json'))
        if [p.name for p in records] != [f'http-{i:02d}.json' for i in range(len(records))] or len(records) > 8:
            raise ValueError('Teacher request slots are not a bounded contiguous history')
        for record in records:
            value = json.loads(record.read_text())
            if value['status'] != 'completed':
                failures[task][record.name] = value
    value = {'authorization': 'user_approved_plan_20261015', 'teacher_tasks': tasks,
             'teacher_failures': failures, 'teacher_turn_limit': 8, 'budget_resets': 0,
             'baseline_extra_dispatches': {task: 1 for task in MISSING_BASELINE},
             'models_and_prompts_unchanged': True, 'stop_on_first_failure': True}
    save(path, value)
    return value


def mark_task(root, task, save):
    amendment_path, amendment = active_amendment(root)
    if task not in amendment['teacher_tasks']:
        raise ValueError('Task outside authorized teacher recovery')
    for name, original in amendment['teacher_failures'][task].items():
        path = root / 'brew' / task / name
        if json.loads(path.read_text()) != original:
            raise ValueError('Original failed teacher record changed')
        marker = path.with_name('recovery-' + name)
        turn = int(path.stem.split('-')[-1])
        value = {'request': original['request'], 'spent_tool_turn': turn,
                 'amendment': str(amendment_path), 'prior_status': original['status'],
                 'outcome_unknown': True, 'usage': original.get('usage'), 'budget_resets': 0}
        if marker.exists():
            previous = json.loads(marker.read_text())
            if previous.get('request') != value['request'] or previous.get('spent_tool_turn') != turn:
                raise ValueError('Existing teacher recovery marker differs')
            if previous.get('amendment') != str(amendment_path):
                archive = marker.with_name('prior-' + amendment_path.stem + '-' + marker.name)
                if not archive.exists():
                    save(archive, previous)
                save(marker, value)
        else:
            save(marker, value)


def preflight(root, adapter, contract, prompts, task_root, save):
    from run_hkuds_upskill_tb21 import ACPTeacher, trace_text, true_reward, trajectory_path
    from tbench_eval.agentbrew.storage import digest, read
    from harbor.models.task.task import Task
    from tbench_eval.agentbrew.storage import text_hash

    baseline, eligible, complete = {}, [], []
    for task in contract['tasks']:
        unit = root / 'baseline' / task
        request = read(unit / 'request.json')
        instruction = (task_root / task / 'instruction.md').read_text()
        expected = {'task': task, 'phase': 'baseline', 'instruction_hash': text_hash(instruction),
                    'task_checksum': Task(task_root / task).checksum,
                    'contract_hash': digest(contract),
                    'retrieval': {'selected': [], 'rendered_system': '',
                                  'retrieval_config': {'max_memory_tokens': None}}}
        if request != expected or not (unit / 'rollout.json').is_file():
            raise ValueError('Baseline cache identity changed: ' + task)
        # Dispatch is prohibited even if a cache read unexpectedly fails.
        original_execute = adapter._execute
        adapter._execute = lambda *a, **k: (_ for _ in ()).throw(ValueError('Preflight cannot dispatch'))
        try:
            baseline[task] = adapter.run(unit, request)
        finally:
            adapter._execute = original_execute
        if true_reward(baseline[task]) is None:
            if task not in MISSING_BASELINE:
                raise ValueError('Unexpected baseline missing: ' + task)
            continue
        eligible.append(task)
        prompt = prompts['brew'](instruction, trace_text(trajectory_path(baseline[task]), 15, 250),
                                'PASS' if true_reward(baseline[task]) else 'FAIL')
        prompt = prompt.replace('(deepseek-v4-pro)', '(deepseek-v4-flash-0731)').replace(
            '(deepseek-v4-flash)', '(qwen3.6-flash-distill)')
        teacher_unit = root / 'brew' / task
        if (teacher_unit / 'prompt.txt').read_text() != prompt:
            raise ValueError('Original Brew prompt differs: ' + task)
        if (teacher_unit / 'acp.json').exists():
            # Rebuild from recorded responses rather than trusting acp.json alone.
            replay_files, messages = {}, [{'role': 'user', 'content': prompt}]
            from run_hkuds_upskill_tb21 import FILES
            for path in sorted(teacher_unit.glob('http-*.json')):
                record = read(path)
                if record['request']['messages'] != messages:
                    raise ValueError('Cached teacher request differs: ' + str(path))
                expected_parameters = {'model': contract['teacher'], 'stream': True,
                    'max_tokens': 65536, 'top_p': .95, 'top_k': 20, 'enable_thinking': True,
                    'reasoning_effort': 'max', 'chat_template_kwargs': {'preserve_thinking': True}}
                if any(record['request'].get(key) != value for key, value in expected_parameters.items()):
                    raise ValueError('Cached teacher parameters differ: ' + str(path))
                if record['status'] != 'completed':
                    continue
                message = record['response']
                messages.append(message)
                for call in message.get('tool_calls') or []:
                    if call['function']['name'] != 'Write':
                        raise ValueError('Unexpected cached tool')
                    arguments = json.loads(call['function']['arguments'])
                    if arguments['file_path'] in FILES:
                        replay_files[FILES[arguments['file_path']]] = arguments['content']
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'content': 'File written successfully.'})
            if replay_files != read(teacher_unit / 'acp.json'):
                raise ValueError('Cached ACP differs from original responses: ' + task)
            complete.append(task)
    report = {'baseline_count': len(baseline), 'valid_baseline': len(eligible),
              'success': sum(true_reward(value) == 1 for value in baseline.values()),
              'complete_acps': len(complete), 'model_requests': 0, 'budget_resets': 0,
              'missing_baseline': sorted(set(baseline) - set(eligible)),
              'teacher_order': teacher_order(root), 'contract_preserved': True}
    save(root / 'recovery_preflight_20261015.json', report)
    print(json.dumps(report), flush=True)
    return baseline


def recover_baseline(root, task, adapter, request, save):
    from run_hkuds_upskill_tb21 import true_reward
    def read(path):
        return json.loads(Path(path).read_text())
    amendment = read(root / 'recovery_amendment_20261015.json')
    if amendment.get('baseline_extra_dispatches', {}).get(task) != 1:
        raise ValueError('No scoped baseline recovery authorization')
    unit = root / 'baseline-recovery-20261015' / task
    original_attempts = adapter.infra_attempts
    adapter.infra_attempts = 1
    try:
        value = adapter.run(unit, request)
    finally:
        adapter.infra_attempts = original_attempts
    if true_reward(value) is None:
        raise ValueError('Authorized baseline recovery still lacks real verifier reward: ' + task)
    raw_path = relocated(value['raw_result_path'])
    raw = read(raw_path)
    reward = ((raw.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    import re
    stdout = (raw_path.parent / 'verifier/test-stdout.txt').read_text(errors='replace')
    reward_file = float((raw_path.parent / 'verifier/reward.txt').read_text().strip())
    exception = (raw.get('exception_info') or {}).get('exception_type')
    if (reward != true_reward(value) or reward_file != reward
            or exception not in (None, 'AgentTimeoutError')
            or 'test session starts' not in stdout
            or not re.search(r'\b[1-9]\d* (?:passed|failed)\b', stdout)):
        raise ValueError('Recovered baseline lacks completed real test/reward evidence: ' + task)
    save(root / 'baseline' / task / 'recovery_selection_20261015.json', {
        'authorization': amendment['authorization'], 'selected_rollout': str(unit / 'rollout.json'),
        'original_rollout': str(root / 'baseline' / task / 'rollout.json'),
        'reward': true_reward(value), 'original_preserved': True})
    return value
