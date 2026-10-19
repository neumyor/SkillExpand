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
    assert a[2].content == b[2].content
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
    assert message in result.validation_error


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
    assert 'JSONDecodeError: Extra data' in result.validation_error
    with pytest.raises(json.JSONDecodeError, match='Extra data'):
        _extract_package_output(raw)


@pytest.mark.parametrize('wrap', [
    lambda text: '```json\n' + text + '\n```',
    lambda text: 'Here is the result:\n' + text,
    lambda text: text + '\nDone.',
    lambda text: text + '\n' + text,
    lambda text: 'The working sequence is:\n```bash\necho solution\n```',
    lambda text: text[:-1] + ',}',
])
def test_package_rejects_non_json_or_extra_content_without_retry(wrap):
    raw = wrap(json.dumps({'body': 'Revised', 'resources': {}, 'rationale': 'Evidence'}))
    calls = []

    def llm(messages, replace_newline=True):
        calls.append(messages)
        assert replace_newline is False
        return raw

    result = SkillEditor(SimpleNamespace(llm=llm), SimpleNamespace(version=0)).propose_package(base(), {})
    assert len(calls) == 1
    assert result.reason == 'invalid_proposal' and result.candidate is None
    assert result.raw_llm_output == raw
    assert result.validation_error.startswith('JSONDecodeError:')


def test_prompt_preserves_full_data_order_and_ends_with_generic_instruction():
    evidence = {'historical_evidence': [{'id': 'trace', 'observation': 'X' * 20000}],
                'public_contract': 'UNIQUE_TASK_INSTRUCTION'}
    feedback = [{'observation': 'Y' * 10000}]
    prompt = editor({}).build_package_prompt(base(), evidence, feedback)
    assert [message.type for message in prompt] == ['system', 'human', 'human']
    payload = json.loads(prompt[1].content)
    assert payload['frozen_evidence'] == evidence
    assert list(payload['frozen_evidence']) == list(evidence)
    assert payload['development_observations'] == feedback
    assert payload['current_skill']['body'] == base().body
    assert payload['current_skill']['resources'] == base().resources
    assert 'not answer or execute any historical task' in prompt[-1].content
    assert 'rationale' in prompt[-1].content and 'same object' in prompt[-1].content
    assert 'UNIQUE_TASK_INSTRUCTION' not in prompt[0].content + prompt[-1].content


def test_package_supports_original_host_signature_and_whitespace():
    value = {'body': 'Revised', 'resources': {}, 'rationale': 'Evidence'}

    def llm(messages, replace_newline=True):
        assert replace_newline is False
        return '\n  ' + json.dumps(value) + '\n'

    result = SkillEditor(SimpleNamespace(llm=llm), SimpleNamespace(version=0)).propose_package(base(), {})
    assert result.proposed
    assert result.validation_error == ''


def test_no_change_requires_its_own_complete_shape():
    result = editor({'no_change': True, 'reason': 'Current rules already cover the evidence'}).propose_package(base(), {})
    assert not result.proposed and result.reason == 'no_effective_change'


def test_prompt_carries_the_same_exact_schema_as_the_offline_validator():
    contract = editor({}).build_package_prompt(base(), {})[0].content
    assert json.dumps(PACKAGE_OUTPUT_SCHEMA, sort_keys=True) in contract
    assert 'do not add no_change:false' in contract.lower()
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
