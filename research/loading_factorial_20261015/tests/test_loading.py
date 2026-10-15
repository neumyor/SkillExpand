import importlib.util
from pathlib import Path
import sys

BASE = Path(__file__).parents[1]
sys.path.insert(0, str(BASE))
from study import schedule, collect_loading_evidence
from skillexpand import schema as S
from skillexpand.runtime.progressive import autonomous_catalog_instruction
import copy
import json
from types import SimpleNamespace
import pytest

for name in ('runtime', 'runner'):
    spec = importlib.util.spec_from_file_location('loading_' + name + '_test', BASE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if name == 'runtime':
        runtime = module
        sys.modules['runtime'] = module
    else:
        runner = module


def test_schedule_exact_balanced_interleaving():
    tasks = [f'task{i}' for i in range(7)]
    rows = schedule(tasks)
    assert len(rows) == 84
    assert rows == schedule(tasks)
    assert len({(r['cell'], r['task'], r['attempt']) for r in rows}) == 84
    for start in range(0, 84, 4):
        assert len({r['cell'] for r in rows[start:start + 4]}) == 4


def test_initial_catalog_does_not_load_bodies_or_resources():
    skills = [S.Skill('p1', 'f', 1, 'name', 'routing text', 'SECRET_BODY',
                      resources={'check.py': 'SECRET_RESOURCE'}, package_mode=True)]
    text = autonomous_catalog_instruction(skills)
    assert 'routing text' in text and '/<skill_id>/SKILL.md' in text
    assert 'SECRET_BODY' not in text and 'SECRET_RESOURCE' not in text
    assert 'none' in text and 'multiple' in text


def test_reference_alone_never_counts_as_read_or_execution():
    path = '/opt/openclaw-skills/p1/SKILL.md'
    manifest = {'skill_ids': ['p1'], 'files': {path: {'skill_id': 'p1',
                'relative_path': 'SKILL.md', 'content': 'A' * 90}}}
    trace = {'steps': [{'step_id': 1, 'tool_calls': [{'arguments': {'keystrokes': 'echo ' + path}}],
                       'observation': {'results': [{'content': path}]}}]}
    value = collect_loading_evidence(trace, [], manifest)
    assert value['read_skill_ids'] == []
    assert value['steps'][0]['observed_contents'] == []


def test_joint_body_and_resource_reads_keep_identity_and_time():
    files = {'/opt/p1/SKILL.md': {'skill_id': 'p1', 'relative_path': 'SKILL.md', 'content': ''},
             '/opt/p2/check.py': {'skill_id': 'p2', 'relative_path': 'check.py', 'content': ''}}
    events = [{'kind': 'access', 'at': 10, 'path': '/opt/p1/SKILL.md'},
              {'kind': 'access', 'at': 20, 'path': '/opt/p2/check.py'}]
    result = collect_loading_evidence({'steps': []}, events, {'files': files, 'skill_ids': ['p1', 'p2']})
    assert result['joint_read_set'] == ['p1', 'p2']
    assert result['body_read_ids'] == ['p1']
    assert result['resource_only_ids'] == ['p2']
    assert [x['at'] for x in result['actual_file_accesses']] == [10, 20]


def test_modes_preserve_common_config_and_verifier(tmp_path):
    template = {'agents': [{'kwargs': {'llm_call_kwargs': {'top_p': .95},
                 'skill_read_suffix': 'legacy'}, 'model_name': 'openai/qwen3.6-flash-distill',
                 'override_timeout_sec': 7200}], 'datasets': [{}], 'environment': {'kwargs': {}},
                 'orchestrator': {'retry': {'max_retries': 3}}, 'verifier': {'disable': False}}
    before = copy.deepcopy(template)
    configs = [runtime.build_config(template, tmp_path, tmp_path / 'slot', 'task', tmp_path / 'skills', mode)
               for mode in ['single', 'autonomous']]
    for config in configs:
        assert config['n_attempts'] == 1 and config['orchestrator']['retry']['max_retries'] == 0
        assert config['verifier'] == template['verifier']
        assert config['agents'][0]['kwargs']['model_info']['max_input_tokens'] == 262144
        config['agents'][0]['kwargs'].pop('loading_mode')
        config['environment']['kwargs'].pop('loading_mode')
    assert configs[0] == configs[1]
    assert template == before


def test_autonomous_never_calls_external_selector_and_mounts_all(tmp_path, monkeypatch):
    skills = [S.Skill('p' + str(i), 'f', 1, 'name', 'description', 'BODY' + str(i)) for i in range(3)]
    monkeypatch.setattr(runner, 'select_and_load', lambda *a: pytest.fail('external selector called'))
    manifest = runner.materialize(tmp_path, skills, 'autonomous', {'instruction': 'task'}, None)
    assert manifest['skill_ids'] == ['p0', 'p1', 'p2']
    assert len(manifest['files']) == 3
    assert 'BODY' not in manifest['initial_suffix']


def test_single_mounts_only_native_choice(tmp_path, monkeypatch):
    skills = [S.Skill('p' + str(i), 'f', 1, 'name', 'description', 'BODY' + str(i)) for i in range(3)]
    monkeypatch.setattr(runner, 'select_and_load', lambda *a: (skills[1], {'skill_id': 'p1'}))
    manifest = runner.materialize(tmp_path, skills, 'single', {'instruction': 'task'}, None)
    assert manifest['skill_ids'] == ['p1']
    assert list(manifest['files']) == ['/opt/openclaw-skills/p1/SKILL.md']


def test_consumed_slot_cannot_be_replaced_and_out_of_schedule_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner.shutil, 'disk_usage', lambda p: SimpleNamespace(free=100 * 1024**3))
    row = {'cell': 'old_autonomous', 'task': 'task', 'attempt': 1}
    runner.write(tmp_path / 'manifest.json', {'stage': 'smoke', 'executor_planned': 1})
    runner.write(tmp_path / 'frozen/schedule.json', [row])
    runner.write(tmp_path / 'bindings.json', [])
    slot, admitted = runner.reserve(row)
    assert admitted
    assert runner.reserve(row) == (slot, False)
    with pytest.raises(ValueError, match='Unallocated'):
        runner.reserve({**row, 'attempt': 2})
