import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from skillexpand import schema as S
from skillexpand.l1 import cold_start as C
from skillexpand.l1.family_discovery import FamilyPlan
from skillexpand.persistence import store as ST
from skillexpand.skill_package import materialize_skill, render_skill_md


def test_real_synthesis_persists_resources_and_reloads_without_model_call(tmp_path):
    cold = object.__new__(C.ColdStart)
    cold.root, cold.directory = tmp_path, tmp_path / 'discovery'
    cold.directory.mkdir()
    cold.plan = S.SplitPlan.make({0: 'train'}, 'terminalbench', 1)
    cold.card_batch_size, cold.skill_edit_mode, cold.initial_skill_format = 1, 'rewrite', 'package'
    card = {'schema_version': 5, 'card_id': 'card', 'task': {'text': 'Public input'},
            'execution': {}, 'evidence': [], 'claims': [], 'claim_status': 'valid'}
    experience = SimpleNamespace(task_id=0, experience_id='card', experience_card=card)
    calls = []
    output = {'description': 'Use for public input validation; excludes unrelated tasks.',
              'body': 'Inspect inputs. Read references/format.md for the format.\nRun python3 scripts/check.py.',
              'resources': {'references/format.md': 'Check the public format.',
                            'scripts/check.py': "print('checked')\n"}}
    cold.ask = lambda prompt: calls.append(prompt) or json.dumps(output)
    cluster = FamilyPlan('terminalbench', 'test', {0: 'family'},
                         {'family': {'task_ids': [0], 'name': 'Public workflow', 'definition': 'Validate inputs'}})
    cold.synthesize([experience], cluster)
    skill = ST.SkillLibrary(tmp_path / 'skills.jsonl', benchmark='terminalbench').head('family')
    assert skill.package_mode and skill.entrypoint_format == 'frontmatter'
    assert skill.resources == output['resources']
    entry = tmp_path / 'initial_packages' / skill.skill_id / 'SKILL.md'
    assert entry.read_text() == render_skill_md(skill)
    metadata = yaml.safe_load(entry.read_text().split('---')[1])
    assert metadata['description'] == skill.description
    assert (entry.parent / 'references/format.md').read_text() == output['resources']['references/format.md']
    import subprocess
    result = subprocess.run(['python3', str(entry.parent / 'scripts/check.py')], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == 'checked\n'
    cold.synthesize([experience], cluster)
    assert len(calls) == 1
    assert 'numbered task-solving rules' not in calls[0]


def test_frontmatter_is_parsed_once_and_conflicts_fail():
    value = {'description': 'Routing: text', 'body': '---\nname: demo\ndescription: "Routing: text"\n---\n\nDo the work.', 'resources': {}}
    normalized = C.normalize_initial_skill(value, package=True)
    assert normalized['body'] == 'Do the work.'
    skill = S.Skill('a', 'a', 0, 'Demo', normalized['description'], normalized['body'],
                    package_mode=True, entrypoint_format='frontmatter')
    assert render_skill_md(skill).count('---') == 2
    with pytest.raises(ValueError, match='agree'):
        C.normalize_initial_skill({**value, 'description': 'Different'}, package=True)
    with pytest.raises(ValueError, match='exactly'):
        C.normalize_initial_skill({'description': 'scope', 'body': 'text'}, package=True)
    with pytest.raises(ValueError):
        C.normalize_initial_skill({**value, 'resources': {'../bad': 'x'}}, package=True)
    with pytest.raises(ValueError, match='Markdown text'):
        C.normalize_initial_skill({**value, 'body': ['Old rule list']}, package=True)


def test_package_revision_preserves_frontmatter_format():
    from skillexpand.l2.editor import SkillEditor

    skill = S.Skill('x', 'family', 0, 'Name', 'Routing', 'Old guide',
                    resources={'references/old.md': 'Evidence'}, package_mode=True,
                    entrypoint_format='frontmatter')
    value = {'body': 'Updated guide', 'resources': skill.resources, 'rationale': 'Observed'}
    editor = SkillEditor(SimpleNamespace(llm=lambda *args, **kwargs: json.dumps(value)),
                         SimpleNamespace(version=0))
    revised = editor.propose_package(skill, {}).candidate.skill
    assert revised.entrypoint_format == 'frontmatter'
    assert revised.package_mode and revised.resources == skill.resources
    assert render_skill_md(revised).startswith('---\n')


def test_old_records_and_materialized_bytes_remain_compatible(tmp_path):
    value = {'skill_id': 'x', 'family_id': 'x', 'version': 0, 'name': 'Name',
             'description': 'Scope', 'body': '1. Old rule.'}
    skill = S.from_dict(S.Skill, value)
    assert not skill.package_mode and skill.entrypoint_format == 'legacy'
    assert 'entrypoint_format' not in S.to_dict(skill)
    assert materialize_skill(skill, tmp_path).read_text() == value['body']
    assert render_skill_md(skill) == '# Name\n\nScope\n\n1. Old rule.\n'


def test_complete_oversize_input_fails_before_model_call(monkeypatch, tmp_path):
    cold = object.__new__(C.ColdStart)
    cold.initial_skill_format = 'package'
    cold.plan = SimpleNamespace(benchmark='terminalbench')
    cold.cfg = SimpleNamespace()
    monkeypatch.setattr(C, 'resolve', lambda cfg: SimpleNamespace(execution_instructions='runtime', tool_semantics=''))
    monkeypatch.setenv('EXPE_LLM_CONTEXT_TOKENS', '2048')
    monkeypatch.setenv('EXPE_LLM_MAX_TOKENS', '1024')
    cold._ask = lambda prompt: pytest.fail('oversized input reached model')
    with pytest.raises(ValueError, match='not truncated'):
        cold.ask('evidence' * 1000)
