"""Offline checks for the frozen model-role experiment matrix."""

import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "model_role_matrix", Path(__file__).resolve().parents[1] / "scripts/model_role_matrix.py"
)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_primary_matrix_is_baseline_plus_four_single_role_conditions():
    plan = M.build_plan("qwen3.6-flash-distill", "glm-5.3-ali")
    assert [item["name"] for item in plan["conditions"]] == [
        "baseline", "l1_executor-strong", "cold_start-strong",
        "l2_planner-strong", "l2_reviewer-strong",
    ]
    assert plan["conditions"][0]["models"] == {
        role: "qwen3.6-flash-distill" for role in M.ROLES
    }
    assert plan["conditions"][1]["models"]["l1_executor"] == "glm-5.3-ali"
    assert all(
        value == "qwen3.6-flash-distill"
        for role, value in plan["conditions"][1]["models"].items()
        if role != "l1_executor"
    )


def test_optional_conditions_are_explicitly_opt_in():
    plan = M.build_plan("base", "strong", include_optional=True)
    assert [item["name"] for item in plan["conditions"]][-3:] == [
        "l2_editor-strong", "l2_verifier-strong", "selector-strong"
    ]
    # The verifier condition must replace the verifier alone.
    verifier = next(item for item in plan["conditions"]
                    if item["name"] == "l2_verifier-strong")
    assert verifier["replaced_roles"] == ["l2_verifier"]
    assert verifier["models"]["l2_verifier"] == "strong"
    assert verifier["models"]["l2_reviewer"] == "base"


def test_prepare_commands_use_existing_campaign_flags(tmp_path):
    plan = M.build_plan("base", "strong")
    commands = M.prepare_commands(plan, tmp_path / "runs", tmp_path / "inputs")
    assert len(commands) == 5
    command = commands[1]
    assert command[command.index("--root") + 1].endswith("/l1_executor-strong")
    assert command[command.index("--l1-model") + 1] == "strong"
    assert command[command.index("--cold-start-model") + 1] == "base"
    assert command[command.index("--predicted-review-scope") + 1] == "val"


def test_test_commands_cover_both_held_out_benchmarks_per_condition(tmp_path):
    plan = M.build_plan("base", "strong")
    commands = M.test_commands(plan, tmp_path / "runs", python="python-under-test")
    assert len(commands) == len(plan["conditions"]) * 2
    assert commands[0][1].endswith("/baseline/code/run_campaign.py")
    assert commands[0][2] == "test"
    assert commands[0][commands[0].index("--benchmark") + 1] == "searchqa"
    assert commands[1][commands[1].index("--benchmark") + 1] == "alfworld"


def test_matrix_validation_rejects_missing_primary_role():
    plan = M.build_plan("base", "strong")
    plan["conditions"] = plan["conditions"][:-1]
    with pytest.raises(ValueError, match="Missing required single-role"):
        M.validate_plan(plan)


def test_prepare_plan_is_written_without_executing_campaigns(tmp_path):
    plan = M.build_plan("base", "strong")
    result = M.prepare(plan, tmp_path / "runs", tmp_path / "inputs", execute=False)
    assert result["prepared"] == []
    saved = json.loads((tmp_path / "runs" / "matrix.json").read_text())
    assert saved["conditions"] == plan["conditions"]
    assert (tmp_path / "runs" / "prepare-commands.json").exists()
    assert len(json.loads((tmp_path / "runs" / "test-commands.json").read_text())["commands"]) == 10


def test_test_complete_requires_audited_complete_summaries(tmp_path):
    root = tmp_path / "condition"
    for benchmark in ("searchqa", "alfworld"):
        target = root / "full" / benchmark / "run" / "test" / "library"
        target.mkdir(parents=True)
        (target / "summary.json").write_text(json.dumps({"status": "complete"}))
        (target / "audit.json").write_text(json.dumps({"integrity": "passed"}))
    assert M.test_complete(root)
    (root / "full" / "alfworld" / "run" / "test" / "library" /
     "summary.json").write_text(json.dumps({"split": "test"}))
    assert not M.test_complete(root)
