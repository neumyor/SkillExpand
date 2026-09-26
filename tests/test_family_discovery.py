"""Validation tests for taxonomy proposal and forced-choice assignment."""

import json
import tempfile
from pathlib import Path

import pytest

from skillexpand.l1 import family_discovery as D


def _fixtures():
    tags = (
        D.TaskTag(0, ("search", "multi-hop reasoning"), "search two entities and join evidence"),
        D.TaskTag(1, ("search", "multi-hop reasoning"), "search two entities and join evidence"),
        D.TaskTag(2, ("comparison",), "retrieve comparable facts and compare them"),
    )
    proposals = (
        D.FamilyProposal("family-p001", "multi-hop search", "search then join evidence",
                         ("requires two linked lookups",)),
        D.FamilyProposal("family-p002", "comparison search", "retrieve and compare facts",
                         ("requires comparison",)),
    )
    assignments = (
        D.FamilyAssignment(0, "family-p001", "direct", "same linked lookup SOP"),
        D.FamilyAssignment(1, "family-p001", "direct", "same linked lookup SOP"),
        D.FamilyAssignment(2, "family-p002", "best_fit", "comparison is the completion contract"),
    )
    return tags, proposals, assignments


def test_family_plan_has_exact_coverage_and_stable_hash():
    tags, proposals, assignments = _fixtures()
    plan = D.make_family_plan("searchqa", tags, proposals, assignments)
    assert plan.families_index == {"family-p001": [0, 1], "family-p002": [2]}
    assert plan.families["family-p001"]["name"] == "multi-hop search"
    assert plan.families["family-p001"]["task_ids"] == [0, 1]
    assert plan.mapping_hash == D.FamilyPlan(
        "searchqa", "forced_choice_assignment",
        {0: "family-p001", 1: "family-p001", 2: "family-p002"},
        plan.families,
    ).mapping_hash


def test_plan_round_trip_writes_taxonomy_and_assignments():
    tags, proposals, assignments = _fixtures()
    plan = D.make_family_plan("searchqa", tags, proposals, assignments)
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory)
        D.write_artifacts(out, plan)
        loaded = D.load_family_plan(out / "family_plan.json", benchmark="searchqa")
        assert loaded.task_to_family == plan.task_to_family
        assert loaded.assignments == plan.assignments
        assert not (out / "membership_audit.json").exists()
        raw = json.loads((out / "family_assignments.json").read_text())
        raw["assignments"].pop()
        with pytest.raises(D.DiscoveryError, match="cover every task"):
            D.parse_assignments(raw, [0, 1, 2], proposals)


def test_proposals_are_taxonomy_only_and_include_all_representative_context():
    tags, _, _ = _fixtures()
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return json.dumps({"families": [
            {"family_id": "family-p001", "name": "lookup", "definition": "lookup and join",
             "trigger_conditions": ["two linked lookups"]},
            {"family_id": "family-p002", "name": "comparison", "definition": "compare facts",
             "trigger_conditions": ["comparison required"]},
        ]})

    proposals = D.propose_families(tags, ask)
    assert [item.family_id for item in proposals] == ["family-p001", "family-p002"]
    assert "candidate_task_ids" not in proposals[0].to_dict()
    assert "exclusion_criteria" not in proposals[0].to_dict()
    assert all(str(task.task_id) in prompts[0] for task in tags)
    assert "Choose the smallest defensible number of families" in prompts[0]


def test_noncanonical_model_family_ids_are_repaired_deterministically():
    tags, _, _ = _fixtures()

    def ask(_prompt):
        return json.dumps({"families": [
            {"family_id": "alfworld_lamp_inspection", "name": "lookup",
             "definition": "lookup and join", "trigger_conditions": ["lookup"]},
            {"family_id": "put-away", "name": "comparison",
             "definition": "compare facts", "trigger_conditions": ["compare"]},
        ]})

    proposals = D.propose_families(tags, ask)
    assert [item.family_id for item in proposals] == ["family-p001", "family-p002"]


def test_old_proposal_fields_are_rejected():
    raw = {"families": [{
        "family_id": "family-p001", "name": "lookup", "definition": "lookup",
        "trigger_conditions": ["lookup"], "candidate_task_ids": [0],
    }]}
    with pytest.raises(D.DiscoveryError, match="must not contain task IDs"):
        D.parse_proposals(raw)

    raw["families"][0].pop("candidate_task_ids")
    raw["families"][0]["exclusion_criteria"] = ["no lookup"]
    with pytest.raises(D.DiscoveryError, match="must not contain task IDs"):
        D.parse_proposals(raw)


def test_assignment_is_per_task_forced_choice_and_sees_complete_taxonomy_and_card():
    tags, proposals, _ = _fixtures()
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        task_id = next(int(value) for value in (str(i) for i in range(10))
                       if f'"task_id": {value}' in prompt)
        family_id = "family-p002" if task_id == 2 else "family-p001"
        match_type = "best_fit" if task_id == 2 else "direct"
        return json.dumps({"task_id": task_id, "family_id": family_id,
                           "match_type": match_type, "rationale": "required operations match"})

    cards = {tag.task_id: {"task": {"text": f"goal-{tag.task_id}"}} for tag in tags}
    assignments = D.assign_families(tags, proposals, ask, batch_size=2, task_cards=cards)
    assert [(item.task_id, item.family_id, item.match_type) for item in assignments] == [
        (0, "family-p001", "direct"), (1, "family-p001", "direct"),
        (2, "family-p002", "best_fit"),
    ]
    assert len(prompts) == 3
    assert all('"families"' in prompt and "family-p002" in prompt for prompt in prompts)
    assert all('"experience_card_projection"' in prompt for prompt in prompts)
    assert all("goal-" in prompt for prompt in prompts)
    assert all("Choose exactly one family" in prompt for prompt in prompts)


def test_assignment_resume_only_calls_pending_tasks_and_never_accepts_unknown_family():
    tags, proposals, _ = _fixtures()
    existing = (D.FamilyAssignment(0, "family-p001", "direct", "checkpoint"),)
    calls = []

    def ask(prompt):
        calls.append(prompt)
        task_id = next(int(value) for value in (str(i) for i in range(10))
                       if f'"task_id": {value}' in prompt)
        return json.dumps({"task_id": task_id, "family_id": "family-p001",
                           "match_type": "best_fit", "rationale": "closest available family"})

    assignments = D.assign_families(tags, proposals, ask, batch_size=1, existing=existing)
    assert [item.task_id for item in assignments] == [0, 1, 2]
    assert len(calls) == 2


def test_parse_assignments_requires_one_valid_assignment_per_task():
    _, proposals, _ = _fixtures()
    with pytest.raises(D.DiscoveryError, match="cover every task"):
        D.parse_assignments({"assignments": []}, [0], proposals)
    with pytest.raises(D.DiscoveryError, match="unknown family"):
        D.parse_assignments({"assignments": [{"task_id": 0, "family_id": "family-p999",
                                               "rationale": "x"}]}, [0], proposals)
    with pytest.raises(D.DiscoveryError, match="match_type"):
        D.parse_assignments({"assignments": [{"task_id": 0, "family_id": "family-p001",
                                               "match_type": "none", "rationale": "x"}]}, [0], proposals)


def test_representatives_deduplicate_capability_signatures():
    tags, _, _ = _fixtures()
    representatives = D.select_representatives(tags, limit=2)
    assert [tag.task_id for tag in representatives] == [0, 2]
    assert "first attempt" in D.FAMILY_CONTRACT
    assert "topic" in D.FAMILY_CONTRACT
