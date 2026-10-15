import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from run_hkuds_upskill_tb21 import ACPTeacher, FILES, complete_audit, trace_text, true_reward, upstream_prompts


def test_unknown_and_framework_zero_are_not_verifier_failures():
    assert true_reward({'reward': 0, 'terminal_failure': True}) is None
    assert true_reward({'reward': 0, 'verifier_evidence': {'status': 'preparation_failed'}}) is None
    assert true_reward({'reward': 0, 'verifier_evidence': {'status': 'tests_executed'}}) == 0
    assert true_reward({'reward': 1, 'verifier_evidence': {'status': 'tests_executed'}}) == 1


def test_trace_projection_matches_upstream(tmp_path):
    p = tmp_path / 'trajectory.json'
    p.write_text(json.dumps({'steps': [{'message': str(i) * 300} for i in range(20)]}))
    value = trace_text(p, 15, 250)
    assert len(value.splitlines()) == 15
    assert all(len(line) == 253 for line in value.splitlines())


def test_official_prompt_is_imported_and_template_extracted():
    source = Path(__file__).resolve().parents[1] / 'sources/HKUDS-UpSkill'
    if not source.exists():
        source = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/sources/HKUDS-UpSkill')
    prompts = upstream_prompts(source)
    spec = importlib.util.spec_from_file_location('upstream', source / 'scripts/run_brew.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert prompts['brew']('task', 'trace', 'PASS') == module.build_brew_prompt('task', 'trace', 'PASS')
    revision = eval(prompts['revision'], {}, {'inst': 'task', 'current_claude': 'current', 'failure_text': 'failed'})
    assert 'REVISE ALL 3 files with Write tool' in revision
    assert '/app/memory/feedback.md' in revision


def test_teacher_executes_three_writes_and_reuses_durable_result(tmp_path, monkeypatch):
    import run_hkuds_upskill_tb21 as runner
    count = []
    def complete(transport, payload, diagnostics=None):
        count.append(payload)
        if len(count) == 1:
            calls = [{'id': str(i), 'type': 'function', 'function': {'name': 'Write',
                     'arguments': json.dumps({'file_path': path, 'content': filename * 50})}}
                     for i, (path, filename) in enumerate(FILES.items())]
            return {'role': 'assistant', 'content': '', 'tool_calls': calls}, {'total_tokens': 1}, 'tool_calls'
        assert sum(m['role'] == 'tool' for m in payload['messages']) == 3
        return {'role': 'assistant', 'content': 'done', 'tool_calls': []}, {}, 'stop'
    monkeypatch.setattr(runner, 'complete_stream', complete)
    def save(path, value):
        Path(path).write_text(json.dumps(value))
    teacher = ACPTeacher(None, 'model', save)
    first = teacher.run(tmp_path, 'prompt')
    assert set(first) == set(FILES.values())
    assert teacher.run(tmp_path, 'prompt') == first and len(count) == 2
    import pytest
    with pytest.raises(ValueError, match='settings differ'):
        ACPTeacher(None, 'different-model', save).run(tmp_path, 'prompt')


def test_missing_ralph_verifier_prevents_complete_acceptance():
    report = {'evo_missing': [], 'serve': {'missing': 0}, 'vanilla': {'missing': 0},
              'ralph_status': {'task': 'failed_after_3'}}
    assert complete_audit(report)
    report['ralph_status']['task'] = 'missing'
    assert not complete_audit(report)
