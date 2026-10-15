import json

from skillexpand.reader.data import Reader


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_agentbrew_phases_retries_notes_and_frozen(tmp_path):
    root = tmp_path / 'external'
    base = root / 'run'
    write(tmp_path / 'runs/external_roots.json', [str(root)])
    write(base / 'contract.json', {})
    write(tmp_path / 'runs/e9/manifest.json', {'stage': 'E9', 'output_root': str(root)})
    unit = base / 'serve/rep00/task-a'
    paths = [unit / f'physical_{i}/jobs/job/trial/agent/trajectory.json' for i in (1, 2)]
    for path in paths:
        write(path, {'steps': [{'step_id': 1, 'source': 'user', 'message': 'hello'}]})
    write(paths[1].parent.parent / 'result.json', {'verifier_result': {'rewards': {'reward': 0}}, 'exception_info': {'exception_type': 'AgentTimeoutError'}})
    write(unit / 'request.json', {'task': 'task-a', 'retrieval': {'memory_generation': 2, 'rendered_system': 'exact injected note'}})
    write(unit / 'rollout.json', {'trajectory_path': str(paths[0])})
    write(base / 'serve/progress.json', [{'task': 'task-a', 'replicate': 0, 'trajectory_path': str(paths[1]), 'raw_result_path': str(paths[1].parent.parent / 'result.json')}])
    write(base / 'vanilla/rep00/task-a/request.json', {'task': 'task-a'})
    brew = base / 'brew/000-task-a'
    write(brew / 'initial/request.json', {'task': 'task-a'})
    write(brew / 'draft/r01/candidate.json', {'note_key': 'candidate', 'corrective_rule': 'try'})
    write(brew / 'ralph/r01/rollout.json', {'trajectory_path': str(paths[1]), 'reward': 0})
    write(brew / 'state.json', {'disposition': 'ralph_exhausted'})
    write(brew / 'curator/decision.json', {'action': 'reject', 'rationale': 'test'})
    note = {'note_key': 'n', 'corrective_rule': 'old'}
    write(base / 'memory/versions/000001.json', {'generation': 1, 'notes': {'n': {'note': note, 'version': 1}}})
    write(base / 'frozen/manifest.json', {'payload': {'memory': {'generation': 2, 'notes': {'n': {'note': {**note, 'corrective_rule': 'frozen'}, 'version': 2}}}}})
    write(base / 'memory/current.json', {'generation': 3, 'notes': {'n': {'note': {**note, 'corrective_rule': 'latest'}, 'version': 3}}})
    reader = Reader(tmp_path / 'runs')
    serve = next(k for k, r in reader.runs.items() if r.manifest.get('stage') == 'serve')
    assert reader.runs[serve].label == 'E9 · AgentBrew · serve'
    vanilla = next(k for k, r in reader.runs.items() if r.manifest.get('stage') == 'vanilla')
    assert reader.same_task(vanilla, 'task-a') == 'task-a'
    traces = reader.traces(serve, 'task-a')
    assert len(traces) == 2
    adopted = next(t for t in traces if t.accepted)
    assert adopted.path == paths[1]
    assert adopted.selection['rendered_system'] == 'exact injected note'
    assert reader.trace_detail(adopted)['reward'] == 0
    assert reader.trace_detail(next(t for t in traces if not t.accepted))['reward'] is None
    notes = reader.skills(serve)
    assert len(notes) == 2
    assert notes[-1]['skill']['body'] == '\n\nfrozen\n\n'
    assert notes[-1]['kind'] == '冻结记忆'
    assert len([c for c in reader.cards(serve) if c.kind == 'AgentBrew 冻结记忆']) == 1
    initial = next(k for k, r in reader.runs.items() if r.manifest.get('stage') == 'initial')
    batch = reader.batches(initial)[0]['data']
    assert batch['acceptance_mode'] == 'real_validation'
    assert batch['acceptance']['curator']['action'] == 'reject'
    assert batch['acceptance']['validation']['reward'] == 0
    assert batch['outcome'] == 'ralph_exhausted'


def test_acp_rerun_discovery_keeps_original(tmp_path):
    old = tmp_path / 'old'
    new = tmp_path / 'E9_hkuds_upskill_acp'
    write(tmp_path / 'runs/e9/manifest.json', {'stage': 'E9', 'output_root': str(old)})
    write(tmp_path / 'runs/e9/status.json', {'output_root': str(new)})
    write(old / 'run/contract.json', {})
    write(old / 'run/brew/000-task/initial/request.json', {'task': 'task'})
    write(new / 'run/contract.json', {'student': 'model', 'phases': ['baseline']})
    write(new / 'run/status.json', {'phase': 'baseline', 'status': 'running'})
    unit = new / 'run/baseline/task'
    write(unit / 'request.json', {'task': 'task', 'retrieval': {'rendered_system': ''}})
    path = unit / 'physical_01/jobs/job/trial/agent/trajectory.json'
    write(path, {'steps': [{'source': 'user', 'message': 'task'}]})
    write(unit / 'rollout.json', {'trajectory_path': str(path)})
    reader = Reader(tmp_path / 'runs')
    runs = list(reader.runs.values())
    assert any(r.label == 'E9 · AgentBrew · initial' for r in runs)
    rerun = next(r for r in runs if r.manifest.get('stage') == 'baseline')
    assert 'E9_hkuds_upskill_acp' in rerun.label
    assert rerun.status['status'] == 'running'
    assert reader.roles(rerun)['executor'] == 'model'
    traces = reader.traces(rerun.key, 'task')
    assert len(traces) == 1 and traces[0].accepted


def test_baseline_recovery_selection_preserves_original_missing(tmp_path):
    root = tmp_path / 'E9'; base = root / 'run'
    write(tmp_path / 'runs/external_roots.json', [str(root)])
    write(base / 'contract.json', {'student': 'model'})
    unit = base / 'baseline/task'
    write(unit / 'request.json', {'task': 'task'})
    original = {'terminal_failure': True, 'reward': 0}
    write(unit / 'rollout.json', original)
    selected = base / 'baseline-recovery-20261015/task/rollout.json'
    trajectory = selected.parent / 'physical_01/jobs/job/trial/agent/trajectory.json'
    result = trajectory.parent.parent / 'result.json'
    write(trajectory, {'steps': []})
    write(result, {'verifier_result': {'rewards': {'reward': 1}}})
    write(selected, {'trajectory_path': str(trajectory), 'raw_result_path': str(result),
        'reward': 1, 'verifier_evidence': {'status': 'tests_executed'}})
    write(unit / 'recovery_selection_20261015.json', {
        'authorization': 'user_approved_plan_20261015', 'selected_rollout': str(selected)})
    reader = Reader(tmp_path / 'runs')
    run = next(r for r in reader.runs.values() if r.manifest.get('stage') == 'baseline')
    assert next(iter(run.slots.values()))['reward'] == 1
    assert reader.traces(run.key, 'task')[0].accepted
    assert json.loads((unit / 'rollout.json').read_text()) == original
