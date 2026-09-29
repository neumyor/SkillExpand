import json

import pytest

from skillexpand import schema as S
from skillexpand.l2.card_review import (
    outcome_effect,
    parse_card_review,
    review_payload,
)


def _base():
    return S.Skill("searchqa.family-p001", "family-p001", 0, "S", "scope", "1. old")


def _card(skill_key, success=False):
    return {
        "card_id": "task-1",
        "execution": {
            "skill_key": skill_key,
            "trials": [{"index": 1, "phase": "autonomous", "success": success}],
        },
        "evidence": [{"id": "t1:e1", "action": "Search[x]", "observation": "seen"}],
    }


def _raw(old, new):
    return json.dumps({"candidates": [{
        "id": "C1", "old_outcome": old, "new_outcome": new,
        "evidence_ids": ["t1:e1"], "rule_ids": ["C1R1"],
        "reason": "Condition: unconditional. Actions differ. Outcome: supported.",
    }]})


def test_relative_effect_is_derived_from_paired_outcomes():
    assert outcome_effect("failure", "success") == "improve"
    assert outcome_effect("success", "failure") == "regress"
    assert outcome_effect("success", "success") == "unchanged"
    assert outcome_effect("failure", "unknown") == "unknown"


def test_reviewer_must_copy_observed_current_outcome():
    base = _base()
    card = _card(base.key, success=False)
    assert review_payload(base, [{"id": "C1", "body": "1. new"}], card)["card"][
        "current_observed_outcome"
    ] == "failure"
    with pytest.raises(ValueError, match="old_outcome"):
        parse_card_review(_raw("success", "success"), base,
                          [{"id": "C1", "body": "1. new"}], card)
    parsed = parse_card_review(_raw("failure", "success"), base,
                               [{"id": "C1", "body": "1. new"}], card)
    assert parsed["C1"]["effect"] == "improve"
    assert parsed["C1"]["old_outcome"] == "failure"


def test_card_from_another_skill_does_not_supply_baseline_outcome():
    base = _base()
    card = _card("searchqa.family-p001@v-1", success=True)
    payload = review_payload(base, [{"id": "C1", "body": "1. new"}], card)
    assert payload["card"]["current_observed_outcome"] == "unknown"
    parsed = parse_card_review(_raw("success", "success"), base,
                               [{"id": "C1", "body": "1. new"}], card)
    assert parsed["C1"]["effect"] == "unchanged"

