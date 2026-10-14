"""Offline checks of selection, loading, evidence boundaries and consumed slots."""

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


PILOT = Path(__file__).resolve().parents[1]
REPO = PILOT.parents[1]
sys.path[:0] = [str(PILOT), str(REPO / "src"), str(REPO / "scripts")]
# Other research tests can import a different module with this same short name.
for name in ("runtime", "input_filter"):
    source = PILOT / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
spec = importlib.util.spec_from_file_location("library_pilot_runner_test", PILOT / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def library():
    return [runner.S.to_dict(runner.S.Skill(
        skill_id=f"terminalbench.family-p{number:03}", family_id=f"family-p{number:03}",
        version=1, name=f"name-{number}", description=f"public routing-{number}",
        body=f"PRIVATE BODY-{number}", package_mode=True,
        resources={"scripts/check.py": f"print({number})\n", "notes/detail.txt": "detail\n"},
    )) for number in (3, 4, 6)]


class Transport:
    def __init__(self, response):
        self.response, self.calls = response, []

    def stream(self, payload):
        self.calls.append(payload)
        event = {"choices": [{"delta": {"content": self.response}, "finish_reason": "stop"}]}
        yield ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda _: SimpleNamespace(free=50 * 1024**3))
    runner.write(tmp_path / "bindings.json", [])
    runner.write(tmp_path / "frozen/panel.json", {
        "development": ["task", "dev-own", "dev-other", "dev-4", "dev-5"],
        "regression": ["regression", "regression-2"],
        "editable_skills": [value["skill_id"] for value in library()],
    })
    runner.write(tmp_path / "frozen/selector_settings.json", {"model": "qwen3.6-flash-distill", "stream": True})
    runner.write(tmp_path / "frozen/executor_template.json", {})
    runner.write(tmp_path / "manifest.json", {"harbor_python": "/fake/python"})
    monkeypatch.setenv("TB21_EXECUTOR_API_KEY", "fake-test-key")
    monkeypatch.setattr(runner, "build_config", lambda *args: {"frozen_execution": True})
    return tmp_path


@pytest.mark.parametrize("selected", ["terminalbench.family-p003", "terminalbench.family-p004", "terminalbench.family-p006"])
def test_real_selector_sees_all_three_and_loads_only_its_chosen_package(experiment, monkeypatch, selected):
    values = library()
    transport = Transport(f"SKILL: {selected}\nWHY: applicable contract")
    calls = []

    def stop_after_load(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("offline stop before Harbor")

    monkeypatch.setattr(runner.subprocess, "run", stop_after_load)
    slot = experiment / "baseline/slots/task/01"
    record = runner.execute(slot, {"task_name": "task", "instruction": "public instruction"}, "baseline", values, transport)
    assert len(transport.calls) == len(calls) == 1
    request = runner.read(slot / "selector_request.json")
    prompt = request["messages"][1]["content"]
    visible = json.loads(prompt.split("SKILLS:\n", 1)[1].split("\nTASK:\n", 1)[0])
    assert visible == [{"skill_id": value["skill_id"], "description": value["description"]} for value in values]
    assert all(set(item) == {"skill_id", "description"} for item in visible)
    assert "PRIVATE BODY" not in prompt and "scripts/check.py" not in prompt
    assert "TASK:\npublic instruction" in prompt
    selected_value = next(value for value in values if value["skill_id"] == selected)
    assert runner.read(slot / "loaded_skill.json") == selected_value
    assert runner.read(slot / "selection.json")["raw"] == transport.response
    manifest = runner.read(slot / "load_manifest.json")
    skill = runner.S.from_dict(runner.S.Skill, selected_value)
    assert manifest["files"] == {"SKILL.md": runner.entrypoint_content(skill, package=True), **skill.resources}
    assert manifest["load_stage"] == "after_selection"
    assert record["selected_skill_id"] == selected
    assert record["status"] == "unresolved" and record["reward"] is None
    assert runner.execute(slot, {"task_name": "task", "instruction": "public instruction"}, "baseline", values, transport) == record
    assert len(transport.calls) == len(calls) == 1


def test_failed_selection_preserves_raw_catalog_and_never_runs_harbor(experiment, monkeypatch):
    transport = Transport("SKILL: terminalbench.unknown\nWHY: bad ID")

    def forbidden(*args, **kwargs):
        pytest.fail("Selection failure must not execute Harbor")

    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    slot = experiment / "baseline/slots/task/01"
    record = runner.execute(slot, {"task_name": "task", "instruction": "instruction"}, "baseline", library(), transport)
    selection = runner.read(slot / "selection.json")
    assert selection["ok"] is False and selection["load_stage"] == "not_loaded"
    assert selection["raw"] == transport.response and len(selection["catalog"]) == 3
    assert record["status"] == "unresolved" and record["reward"] is None
    assert not (slot / "loaded_skill.json").exists()
    assert not (slot / "skills").exists()


def evidence_slot(root, directory, task, phase, selected, content="observed result"):
    slot = root / directory / "slots" / task / "01"
    skill = next(value for value in library() if value["skill_id"] == selected)
    runner.write(slot / "loaded_skill.json", skill)
    trace = slot / "trajectory.json"
    runner.write(trace, {"steps": [{"step_id": 1, "tool_calls": [{"function_name": "terminal", "arguments": {"command": "cat /app/data"}}],
                                  "observation": {"results": [{"content": content}]}}]})
    runner.write(slot / "record.json", {"task": task, "phase": phase, "selected_skill_id": selected,
                  "loaded_skill_key": selected + "@v1", "status": "valid", "reward": 1,
                  "trajectory_path": str(trace)})
    return slot


@pytest.mark.parametrize("selected", ["terminalbench.family-p003", "terminalbench.family-p004", "terminalbench.family-p006"])
def test_evidence_aggregates_actual_choice_for_every_editable_skill_and_keeps_full_observation(experiment, selected):
    long_content = "START\n" + "x" * 40000 + "\nEND"
    own = evidence_slot(experiment, "baseline", "dev-own", "baseline", selected, long_content)
    other = next(value["skill_id"] for value in library() if value["skill_id"] != selected)
    evidence_slot(experiment, "baseline", "dev-other", "baseline", other)
    evidence_slot(experiment, "baseline", "regression", "baseline", selected)
    tasks = [{"task_name": "dev-own", "instruction": "own contract", "split": "development"},
             {"task_name": "dev-other", "instruction": "other contract", "split": "development"},
             {"task_name": "regression", "instruction": "regression contract", "split": "regression"}]
    evidence, paths = runner.evidence_for(selected, [experiment / "baseline"], tasks)
    assert paths == [str(own.relative_to(experiment))]
    assert evidence["public_contracts"] == [{"task_name": "dev-own", "public_contract": "own contract"}]
    rows = evidence["historical_evidence"]
    assert len(rows) == 1 and rows[0]["selected_skill_id"] == selected
    result = rows[0]["tool_observations"][0]["results"][0]
    assert result["content"] == long_content and result["truncated"] is False


def test_final_development_results_cannot_be_aggregated(experiment):
    selected = "terminalbench.family-p004"
    evidence_slot(experiment, "final", "dev-own", "final", selected)
    tasks = [{"task_name": "dev-own", "instruction": "contract", "split": "development"}]
    try:
        evidence, paths = runner.evidence_for(selected, [experiment / "final"], tasks)
    except ValueError:
        return
    assert paths == [] and evidence["historical_evidence"] == []


@pytest.mark.parametrize("kind,maximum", [("executor", 62), ("generation", 6)])
def test_reservation_budget_is_fixed_and_consumed_slots_are_not_retried(experiment, kind, maximum):
    panel = runner.read(experiment / "frozen/panel.json")
    if kind == "executor":
        scheduled = [experiment / phase / "slots" / task / attempt
                     for phase in ("baseline", "final")
                     for task in panel["development"] + panel["regression"]
                     for attempt in ("01", "02", "03")]
        scheduled += [experiment / "candidates" / candidate / "dev/slots" / task / attempt
                      for candidate in ("001", "002") for task in panel["development"]
                      for attempt in ("01", "02")]
    else:
        scheduled = [experiment / "candidates" / candidate / "generation" / skill_id
                     for candidate in ("001", "002") for skill_id in panel["editable_skills"]]
    assert len(scheduled) == maximum
    for directory in scheduled:
        assert runner.reserve(directory, kind) is True
        assert runner.reserve(directory, kind) is False
        assert runner.read(directory / "reserved.json")["no_retry"] is True
    with pytest.raises(ValueError, match="outside frozen"):
        runner.reserve(experiment / kind / "excess", kind)
    (scheduled[-1] / "reserved.json").unlink()
    runner.write(experiment / "unexpected_consumed/reserved.json", {"kind": kind})
    with pytest.raises(ValueError, match="budget exhausted"):
        runner.reserve(scheduled[-1], kind)


def test_interrupted_reserved_executor_remains_unresolved_without_new_selector_call(experiment):
    slot = experiment / "baseline/slots/task/01"
    assert runner.reserve(slot, "executor") is True
    transport = Transport("SKILL: terminalbench.family-p003\nWHY: test")
    record = runner.execute(slot, {"task_name": "task", "instruction": "instruction"}, "baseline", library(), transport)
    assert record["error_type"] == "InterruptedConsumedSlot"
    assert record["status"] == "unresolved" and record["reward"] is None
    assert transport.calls == []
