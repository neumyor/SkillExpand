"""Offline checks for paired model-role result reconstruction."""

import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "model_role_summary",
    Path(__file__).resolve().parents[1] / "scripts/summarize_model_role_matrix.py",
)
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)


def artifact(outcomes, routes):
    return {
        "score": sum(outcomes.values()) / len(outcomes),
        "outcomes": outcomes,
        "route_assignment": routes,
    }


def test_paired_rebuild_includes_task_ids_route_changes_and_mcnemar():
    before = artifact({0: True, 1: False, 2: False}, {0: "a", 1: "a", 2: "b"})
    after = artifact({0: False, 1: True, 2: False}, {0: "a", 1: "b", 2: "b"})
    result = S.paired(before, after)
    assert result["cells"]["correct_to_wrong"] == {"count": 1, "task_ids": [0]}
    assert result["cells"]["wrong_to_correct"] == {"count": 1, "task_ids": [1]}
    assert result["route_changes"] == {1: {"before": "a", "after": "b"}}
    assert result["routed_only"]["tasks"] == 3
    assert result["same_route_only"]["tasks"] == 2
    assert result["mcnemar"]["exact_two_sided_p"] == 1.0


def test_mcnemar_exact_handles_no_discordant_pairs():
    assert S.mcnemar_exact(0, 0) == 1.0


def test_paired_separates_routing_failures_from_execution_pairs():
    before = artifact({0: True, 1: False}, {0: "a", 1: None})
    after = artifact({0: False, 1: True}, {0: "b", 1: "a"})
    result = S.paired(before, after)
    assert result["route_failure_task_ids"] == {"before": [1], "after": []}
    assert result["routed_only"]["tasks"] == 1
    assert result["routed_only"]["cells"]["correct_to_wrong"]["task_ids"] == [0]
    assert result["same_route_only"]["tasks"] == 0
