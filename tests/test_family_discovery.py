"""Pure validation tests for the CPM-style family discovery boundary."""

import json
import re
import tempfile
from pathlib import Path

import pytest

from skillexpand.l1 import family_discovery as D
from skillexpand.evaluation import splits as S


def _fixtures():
    tags = (
        D.TaskTag(0, ('search', 'multi-hop reasoning'), 'search two entities and join evidence'),
        D.TaskTag(1, ('search', 'multi-hop reasoning'), 'search two entities and join evidence'),
        D.TaskTag(2, ('comparison',), 'retrieve comparable facts and compare them'),
    )
    proposals = (
        D.FamilyProposal('family-p001', 'multi-hop search', 'search then join evidence',
                         ('requires two linked lookups',), ('single lookup only',), (0, 1)),
        D.FamilyProposal('family-p002', 'comparison search', 'retrieve and compare facts',
                         ('requires comparison',), ('no comparison',), (2,)),
    )
    audits = (
        D.MembershipAudit(0, ('family-p001',), 'family-p001', 'same linked lookup SOP'),
        D.MembershipAudit(1, ('family-p001',), 'family-p001', 'same linked lookup SOP'),
        D.MembershipAudit(2, ('family-p002',), 'family-p002', 'comparison is the completion contract'),
    )
    return tags, proposals, audits


def test_family_plan_has_exact_coverage_and_stable_hash():
    tags, proposals, audits = _fixtures()
    plan = D.make_family_plan('searchqa', tags, proposals, audits)
    assert plan.families_index == {'family-p001': [0, 1], 'family-p002': [2]}
    assert plan.mapping_hash == D.FamilyPlan(
        'searchqa', 'capability_audit', {0: 'family-p001', 1: 'family-p001', 2: 'family-p002'},
        plan.families).mapping_hash


def test_plan_round_trip_and_reject_missing_membership():
    tags, proposals, audits = _fixtures()
    plan = D.make_family_plan('searchqa', tags, proposals, audits)
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory)
        D.write_artifacts(out, plan)
        loaded = D.load_family_plan(out / 'family_plan.json', benchmark='searchqa')
        assert loaded.task_to_family == plan.task_to_family
        raw = json.loads((out / 'membership_audit.json').read_text())
        raw['audits'].pop()
        try:
            D.parse_audits(raw, [0, 1, 2], proposals)
        except D.DiscoveryError:
            pass
        else:
            raise AssertionError('incomplete audit was accepted')


def test_discovery_pipeline_uses_task_level_tags_then_audit():
    replies = [
        '{"capability_tags":["lookup"],"capability_summary":"lookup and answer"}',
        '{"capability_tags":["lookup"],"capability_summary":"lookup and answer"}',
        '{"families":[{"family_id":"family-p001","label":"lookup","definition":"lookup and answer",'
        '"inclusion_criteria":["lookup"],"exclusion_criteria":["no lookup"],"candidate_task_ids":[0,1]}]}',
        '{"audits":[{"task_id":0,"candidate_family_ids":["family-p001"],"family_id":"family-p001",'
        '"rationale":"same SOP"}]}',
        '{"audits":[{"task_id":1,"candidate_family_ids":["family-p001"],"family_id":"family-p001",'
        '"rationale":"same SOP"}]}',
    ]
    it = iter(replies)
    tags = D.tag_tasks({0: 'q0', 1: 'q1'}, lambda _: next(it))
    proposals = D.propose_families(tags, lambda _: next(it))
    audits = D.audit_families(tags, proposals, lambda _: next(it))
    assert D.make_family_plan('searchqa', tags, proposals, audits).families_index == {
        'family-p001': [0, 1]
    }


def test_representative_proposals_expand_and_audit_in_batches():
    tags, proposals, _ = _fixtures()
    representatives = D.select_representatives(tags, limit=2)
    # The proposal fixture uses all representative IDs for this check.
    proposal = D.FamilyProposal(
        'family-p001', 'lookup', 'lookup SOP', ('lookup',), ('none',),
        tuple(tag.task_id for tag in representatives),
    )
    expanded = D.expand_proposals((proposal,), representatives, tags)
    assert expanded[0].candidate_task_ids == (0, 1, 2)

    replies = iter([
        '{"audits":[{"task_id":0,"candidate_family_ids":["family-p001"],'
        '"family_id":"family-p001","rationale":"same"}]}',
        '{"audits":[{"task_id":1,"candidate_family_ids":["family-p001"],'
        '"family_id":"family-p001","rationale":"same"}]}',
        '{"audits":[{"task_id":2,"candidate_family_ids":["family-p001"],'
        '"family_id":"family-p001","rationale":"same"}]}',
    ])
    audits = D.audit_families(tags, expanded, lambda _: next(replies), batch_size=1)
    assert [audit.task_id for audit in audits] == [0, 1, 2]


def test_proposal_retries_when_a_representative_capability_is_missing():
    tags = (
        D.TaskTag(0, ('lamp',), 'use a lamp'),
        D.TaskTag(5, ('placement',), 'move one object to a receptacle'),
    )
    replies = iter([
        json.dumps({'families': [{
            'family_id': 'family-p001', 'label': 'Lamp', 'definition': 'use a lamp',
            'inclusion_criteria': ['lamp required'], 'exclusion_criteria': ['no lamp'],
            'candidate_task_ids': [0],
        }]}),
        json.dumps({'families': [
            {'family_id': 'family-p001', 'label': 'Lamp', 'definition': 'use a lamp',
             'inclusion_criteria': ['lamp required'], 'exclusion_criteria': ['no lamp'],
             'candidate_task_ids': [0]},
            {'family_id': 'family-p002', 'label': 'Placement',
             'definition': 'move one object to a receptacle',
             'inclusion_criteria': ['one object'], 'exclusion_criteria': ['lamp required'],
             'candidate_task_ids': [5]},
        ]}),
    ])
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return next(replies)

    proposals = D.propose_families(tags, ask)
    assert len(proposals) == 2
    assert 'omit task ids: [5]' in prompts[1]
    assert 'Add a family' in prompts[1]


def test_incomplete_proposal_cannot_be_hidden_by_candidate_expansion():
    tags = (
        D.TaskTag(0, ('lamp',), 'use a lamp'),
        D.TaskTag(5, ('placement',), 'move one object'),
    )
    raw = {'families': [{
        'family_id': 'family-p001', 'label': 'Lamp', 'definition': 'use a lamp',
        'inclusion_criteria': ['lamp required'], 'exclusion_criteria': ['no lamp'],
        'candidate_task_ids': [0],
    }]}
    with pytest.raises(D.DiscoveryError, match=r'omit task ids: \[5\]'):
        D.parse_proposals(raw, [0, 5])
    proposals = D.parse_proposals(raw, [0])
    expanded = D.expand_proposals(proposals, tags, tags)
    assert expanded[0].candidate_task_ids == (0, 5)


def test_membership_audit_stops_on_a_missing_family_instead_of_forcing_a_choice():
    tag = D.TaskTag(5, ('placement',), 'move one object')
    proposals = (D.FamilyProposal(
        'family-p001', 'Lamp', 'use a lamp', ('lamp required',),
        ('no lamp',), (5,)),)
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return json.dumps({'audits': [{
            'task_id': 5, 'candidate_family_ids': ['family-p001'],
            'family_id': None, 'rationale': 'Single-object placement does not use a lamp',
        }]})

    with pytest.raises(D.UncoveredFamilyError, match='no proposed family fits task 5'):
        D.audit_families((tag,), proposals, ask, batch_size=1)
    assert len(prompts) == 3
    assert 'Never force a task into an incompatible family' in prompts[0]
    assert 'Single-object placement does not use a lamp' in prompts[1]


def test_membership_audit_rechecks_initial_null_against_task_evidence():
    tag = D.TaskTag(1, ('examine',), 'examine a CD with a lamp')
    proposals = (D.FamilyProposal(
        'family-p001', 'Examine', 'use a lamp to inspect an object',
        ('examine an object with a lamp',), ('no lamp',), (1,)),)
    replies = iter((
        {'audits': [{'task_id': 1, 'candidate_family_ids': ['family-p001'],
                     'family_id': None, 'rationale': 'the CD was not held in the first attempt'}]},
        {'audits': [{'task_id': 1, 'candidate_family_ids': ['family-p001'],
                     'family_id': 'family-p001', 'rationale': 'the goal requires lamp inspection'}]},
    ))
    prompts = []
    def ask(prompt):
        prompts.append(prompt)
        return json.dumps(next(replies))
    audits = D.audit_families((tag,), proposals, ask, batch_size=1,
                              task_cards={1: {'goal': 'examine the cd with the desklamp'}})
    assert audits[0].family_id == 'family-p001'
    assert len(prompts) == 2
    assert 'the CD was not held in the first attempt' in prompts[1]


def test_audit_rejects_fallback_and_splits_failed_batches():
    tags = tuple(D.TaskTag(i, ('lookup',), 'lookup') for i in range(4))
    proposals = (D.FamilyProposal(
        'family-p001', 'lookup', 'lookup SOP', ('lookup',), ('none',),
        (0, 1, 2, 3)),)
    calls = []

    def ask(prompt):
        calls.append(prompt)
        if len(calls) <= 3:
            return 'not json'
        ids = [int(value) for value in re.findall(r'"task_id":\s*(\d+)', prompt)]
        return json.dumps({'audits': [
            {'task_id': task_id, 'candidate_family_ids': ['family-p001'],
             'family_id': 'family-p001', 'rationale': 'same SOP'}
            for task_id in ids
        ]})

    audits = D.audit_families(tags, proposals, ask, batch_size=4)
    assert [audit.task_id for audit in audits] == [0, 1, 2, 3]
    assert len(calls) > 3  # the failed batch was split and recovered

    try:
        D.parse_audits({'audits': [{
            'task_id': 0, 'candidate_family_ids': ['family-p001'],
            'family_id': 'family-p001',
            'rationale': 'DETERMINISTIC_FALLBACK: guessed',
        }]}, [0], proposals)
    except D.DiscoveryError:
        pass
    else:
        raise AssertionError('deterministic fallback audit was accepted')


def test_candidate_expansion_cannot_hide_a_semantically_valid_family():
    tags=(D.TaskTag(0,('lookup',),'lookup'),D.TaskTag(1,('format',),'format'),D.TaskTag(214,('lookup',),'name constraint'))
    proposals=(D.FamilyProposal('family-p001','Lookup','lookup',('lookup',),('none',),(0,)),
               D.FamilyProposal('family-p002','Name form','name',('name',),('none',),(1,)))
    expanded=D.expand_proposals(proposals,tags[:2],tags)
    assert all(p.candidate_task_ids==(0,1,214) for p in expanded)
    assert 'first attempt' in D.FAMILY_CONTRACT
    assert 'inclusion/exclusion criteria' in D.FAMILY_CONTRACT
