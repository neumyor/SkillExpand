"""Native package proposals preserve routing and validate full resources."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillexpand import schema as S
from skillexpand.l2.editor import PACKAGE_OUTPUT_SCHEMA, SkillEditor, validate_package_output


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


@pytest.mark.parametrize('value,message', [
    ({'body': 'Revised'}, 'missing fields: rationale, resources'),
    ({'body': 'Revised', 'resources': {}, 'rationale': 'Evidence', 'no_change': False},
     'unexpected fields: no_change'),
    ({'body': 'Revised', 'resources': {}, 'rationale': '  '}, 'nonempty text: rationale'),
    ({'body': 'Revised', 'resources': [], 'rationale': 'Evidence'}, 'map relative paths'),
    ({'body': 'Revised', 'resources': {'check.sh': 3}, 'rationale': 'Evidence'}, 'map relative paths'),
    ({'no_change': True}, 'missing fields: reason'),
    ({'no_change': True, 'reason': 'Already covered', 'body': 'Contradictory'}, 'unexpected fields: body'),
])
def test_strict_output_validator_diagnoses_missing_or_ambiguous_semantics(value, message):
    before = json.dumps(value)
    with pytest.raises(ValueError, match=message):
        validate_package_output(value)
    assert json.dumps(value) == before
    result = editor(value).propose_package(base(), {})
    assert not result.proposed and result.reason == 'invalid_proposal'
    assert result.raw_llm_output == before


def test_valid_output_preserves_exact_resource_and_rationale_strings():
    value = {'body': 'Revised\\n literal sequence\n actual newline',
             'resources': {'check.sh': 'printf "%s\\n" "quoted"\n'},
             'rationale': '  Exact evidence rationale  '}
    assert validate_package_output(value) is value
    result = editor(value).propose_package(base(), {})
    assert result.proposed
    assert result.candidate.skill.body == value['body']
    assert result.candidate.skill.resources == value['resources']
    assert result.candidate.skill.provenance.rationale == value['rationale']


def test_adjacent_package_objects_are_rejected_instead_of_merging_fields():
    raw = json.dumps({'body': 'Revised', 'resources': {}}) + '\n' + json.dumps({
        'rationale': 'Evidence'})
    result = SkillEditor(SimpleNamespace(llm=lambda *a, **kw: raw),
                         SimpleNamespace(version=0)).propose_package(base(), {})
    assert not result.proposed
    assert result.reason == 'invalid_proposal'

    from skillexpand.l2.editor import _extract_package_output
    with pytest.raises(ValueError, match='one complete JSON object'):
        _extract_package_output(raw)


def test_no_change_requires_its_own_complete_shape():
    result = editor({'no_change': True, 'reason': 'Current rules already cover the evidence'}).propose_package(base(), {})
    assert not result.proposed and result.reason == 'no_effective_change'


def test_prompt_carries_the_same_exact_schema_as_the_offline_validator():
    contract = editor({}).build_package_prompt(base(), {})[0].content
    assert json.dumps(PACKAGE_OUTPUT_SCHEMA, sort_keys=True) in contract
    assert 'do not add no_change:false' in contract
    assert 'Never omit resources' in contract
    assert 'invent a rationale' in contract


@pytest.mark.parametrize('skill_id,expected_fields', [
    ('terminalbench.family-p004', {'body', 'resources', 'rationale', 'no_change'}),
    ('terminalbench.family-p006', {'body'}),
])
def test_saved_failed_responses_are_still_rejected_without_synthesizing_fields(skill_id, expected_fields):
    archive = os.environ.get('SKILLEXPAND_PACKAGE_FAILURE_ARCHIVE')
    if not archive:
        pytest.skip('Set SKILLEXPAND_PACKAGE_FAILURE_ARCHIVE for immutable saved-response replay')
    folder = Path(archive) / 'candidates/002/generation' / skill_id
    completion = json.loads((folder / 'completion.json').read_text())
    previous = json.loads((folder / 'outcome.json').read_text())
    raw = completion['choices'][0]['message']['content']
    value = json.loads(raw)
    assert set(value) == expected_fields
    assert previous['raw_llm_output'] == raw and previous['reason'] == 'invalid_proposal'
    with pytest.raises(ValueError):
        validate_package_output(value)
    ed = SkillEditor(SimpleNamespace(llm=lambda *a, **kw: raw), SimpleNamespace(version=0))
    result = ed.propose_package(base(), {})
    assert result.reason == 'invalid_proposal' and result.candidate is None
    assert result.raw_llm_output == raw
