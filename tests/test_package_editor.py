"""Native package proposals preserve routing and validate full resources."""
import json
from types import SimpleNamespace

from skillexpand import schema as S
from skillexpand.l2.editor import SkillEditor


def editor(value):
    return SkillEditor(SimpleNamespace(llm=lambda *a, **kw: json.dumps(value)),
                       SimpleNamespace(version=0))


def base():
    return S.Skill('skill', 'family', 1, 'Name', 'Frozen routing', 'Existing',
                   resources={'references/old.txt': 'Old'})


def test_resource_only_revision_and_non_python_package():
    value = {'body': 'Existing', 'resources': {'scripts/check.sh': 'exit 0'},
             'rationale': 'Observed evidence'}
    result = editor(value).propose_package(base(), {'historical_evidence': [{'id': 'trace'}]})
    assert result.proposed
    assert result.candidate.skill.description == base().description
    assert result.candidate.skill.resources == value['resources']
    assert result.candidate.skill.provenance.source_experience_ids == ('trace',)


def test_unsafe_resources_and_unchanged_package_rejected():
    value = {'body': 'Existing', 'resources': {'../outside': 'bad'}, 'rationale': 'Evidence'}
    assert not editor(value).propose_package(base(), {}).proposed
    value['resources'] = base().resources
    assert not editor(value).propose_package(base(), {}).proposed


def test_public_contract_is_data_and_language_is_not_prescribed():
    ed = editor({})
    a = ed.build_package_prompt(base(), {'public_contract': 'Transform CSV records'})
    b = ed.build_package_prompt(base(), {'public_contract': 'Serve an HTTP endpoint'})
    assert a[0].content == b[0].content
    assert 'Transform CSV records' not in a[0].content
    assert json.loads(a[1].content)['frozen_evidence']['public_contract'] == 'Transform CSV records'
