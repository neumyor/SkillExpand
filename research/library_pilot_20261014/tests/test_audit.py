"""Synthetic saved artifacts exercise native replay without model or sandbox calls."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest


RESEARCH = Path(__file__).resolve().parents[1]
REPO = RESEARCH.parents[1]
spec = importlib.util.spec_from_file_location("library_audit_tested", RESEARCH / "audit.py")
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def stream(path, raw, native):
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": raw}, "finish_reason": "stop"}]}
    path.write_text("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
    return native.stream_completion(path)


def saved_trial(root, native, table, template, settings, relative, phase, library, chosen=None, reward=1):
    slot = root / relative
    task = table[relative.split("/")[-2]]
    put(slot / "reserved.json", {"kind": "executor", "no_retry": True})
    put(slot / "input.json", {"task": task, "phase": phase, "library": library})
    catalog = [{"skill_id": row["skill_id"], "description": row["description"]}
               for row in sorted(library, key=lambda row: row["skill_id"])]
    put(slot / "catalog.json", catalog)
    put(slot / "selector_request.json", {**settings, "messages": native.selector_messages(task["instruction"], library)})
    raw = f"SKILL: {chosen}\nWHY: operation match" if chosen else "unparsable choice"
    put(slot / "selector_response.json", {**stream(slot / "selector_response.sse", raw, native), "raw": raw})
    selection = {"ok": bool(chosen), "raw": raw, "catalog": catalog, "skill_id": chosen,
                 "why": "operation match" if chosen else "", "load_stage": "after_selection" if chosen else "not_loaded"}
    record = {"status": "unresolved", "reward": None, "no_retry": True, "error_type": "SelectorError"}
    if chosen:
        skill = next(row for row in library if row["skill_id"] == chosen)
        key = f"{chosen}@v{skill['version']}"
        selection.update(loaded_skill_id=chosen, loaded_skill_key=key)
        put(slot / "loaded_skill.json", skill)
        config = native.runtime.build_config(template, root, slot, task["task_name"], slot / "skills")
        put(slot / "harbor_config.json", config)
        package = native.skill(skill)
        files = {"SKILL.md": native.entrypoint(package, package=package.package_mode)}
        if package.package_mode:
            files.update(package.resources)
        source = slot / "skills" / task["task_name"] / "SKILL.md"
        for name, content in files.items():
            path = source.parent / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        target = f"/opt/openclaw-skills/{task['task_name']}/SKILL.md"
        put(slot / "load_manifest.json", {"source_path": str(source), "skill_path": target, "files": files})
        trial = slot / "jobs/run" / (task["task_name"] + "__fresh")
        trajectory = {"final_metrics": {"total_prompt_tokens": 12, "total_completion_tokens": 5},
                      "steps": [{"tool_calls": [{"function_name": "shell", "arguments": {"command": "cat input.csv"}}],
                                 "observation": {"results": [{"content": "public data " * 3000}]}}]}
        put(trial / "agent/trajectory.json", trajectory)
        identity = "sandbox-" + relative.replace("/", "-")
        put(trial / "snapshot.json", {"source_sandbox_id": identity})
        put(trial / "agent/skill_activation.json", {"load_stage": "after_selection", "source_path": str(source), "skill_path": target})
        put(trial / "agent/effective_agent_config.json", {"max_input_tokens": 262144, "max_output_tokens": 65536,
            "enable_summarize": True, "proactive_summarization_threshold": 8000, "skills_dir": "/opt/openclaw-skills", "api_base": "synthetic"})
        put(trial / "result.json", {"task_name": task["task_name"], "exception_info": None,
              "verifier_result": {"rewards": {"reward": reward}},
              "config": {"agent": {**template["agents"][0], "name": "terminus-2"}, "verifier": template["verifier"],
                         "task": {"path": str(root / "frozen/tasks" / task["task_name"])},
                         "environment": config["environment"]}})
        (trial / "verifier").mkdir()
        (trial / "verifier/reward.txt").write_text(str(reward))
        (trial / "verifier/test-stdout.txt").write_text("test session starts\n1 passed")
        record.update(status="valid", reward=reward, selected_skill_id=chosen, loaded_skill_key=key,
                      sandbox_id=identity, result_path=str(trial / "result.json"), trajectory_path=str(trial / "agent/trajectory.json"))
    put(slot / "selection.json", selection)
    put(slot / "record.json", record)


@pytest.fixture
def saved_run(tmp_path):
    root = tmp_path / "run"
    (root / "code").mkdir(parents=True)
    (root / "code/skillexpand").symlink_to(REPO / "src/skillexpand", target_is_directory=True)
    shutil.copy(REPO / "scripts/summarize_tb21_empirical.py", root / "code/summarize_tb21_empirical.py")
    shutil.copy(RESEARCH / "input_filter.py", root / "input_filter.py")
    shutil.copy(RESEARCH / "runtime.py", root / "runtime.py")
    native = audit.NativeReplay(root)
    names = ["dev1", "dev2", "dev3", "dev4", "dev5", "reg1", "reg2"]
    table = {name: {"task_name": name, "instruction": "Produce the specified public artifact.",
                   "split": "development" if index < 5 else "regression"} for index, name in enumerate(names)}
    seed = [{"skill_id": f"terminalbench.family-p00{index}", "family_id": f"family-p00{index}",
             "name": f"Skill {index}", "description": f"Operation {index}", "body": "Follow public requirements.",
             "version": 1, "provenance": {}} for index in (3, 4, 6)]
    chosen = seed[1]["skill_id"]
    template = {"agents": [{"name": None, "model_name": "openai/qwen3.6-flash-distill",
                            "import_path": "library_pilot_runtime:TencentSkillTerminus2",
                            "kwargs": {"model_info": {"max_input_tokens": 262144, "max_output_tokens": 65536},
                                       "enable_summarize": True, "proactive_summarization_threshold": 8000,
                                       "skills_dir": "/opt/openclaw-skills", "record_terminal_session": False,
                                       "record_terminal_output": False}}],
                "datasets": [{"path": "placeholder", "task_names": []}], "n_attempts": 1,
                "environment": {"import_path": "library_pilot_runtime:LibraryPilotEnvironment", "kwargs": {}},
                "orchestrator": {"retry": {"max_retries": 0}, "n_concurrent_trials": 1}, "verifier": {"disable": False}}
    settings = {"model": "synthetic", "max_tokens": 65536}
    put(root / "manifest.json", {"remote_root": str(root)})
    put(root / "frozen/panel.json", {"development": names[:5], "regression": names[5:]})
    put(root / "frozen/task_table.json", list(table.values()))
    put(root / "frozen/seed_library.json", seed)
    put(root / "frozen/executor_template.json", template)
    put(root / "frozen/selector_settings.json", settings)
    put(root / "frozen/generation_settings.json", settings)
    put(root / "frozen/historical_sandbox_ids.json", ["historical-sandbox"])
    (root / "frozen/sentinel").write_bytes(b"frozen bytes")
    (root / "frozen/snapshot").write_bytes(b"frozen bytes")
    put(root / "bindings.json", [{"current": "frozen/sentinel", "snapshot": "frozen/snapshot"}])
    for name in names:
        for index in range(1, 4):
            saved_trial(root, native, table, template, settings, f"baseline/slots/{name}/{index:02d}", "baseline", seed,
                        chosen if name == "dev1" and index == 1 else None)
    current = seed
    evidence_sources = ["baseline/slots/dev1/01"]
    for number in (1, 2):
        folder = root / "candidates" / f"{number:03d}"
        library = list(current)
        for index, skill in enumerate(current):
            directory = folder / "generation" / skill["skill_id"]
            if skill["skill_id"] != chosen:
                put(directory / "generation_status.json", {"status": "skipped_no_selected_evidence", "no_retry": True})
                continue
            rows = []
            for source in evidence_sources:
                slot = root / source
                record = audit.read(slot / "record.json")
                trace = audit.read(record["trajectory_path"])
                rows.append({"id": source, "task_name": "dev1", "selected_skill_id": chosen,
                             "loaded_skill_key": record["loaded_skill_key"], "official_reward": record["reward"],
                             "loaded_skill": audit.read(slot / "loaded_skill.json"),
                             "tool_observations": native.filter.project_tool_results(trace)})
            value = {"base_skill": skill, "feedback": [], "source_slots": evidence_sources,
                     "evidence": {"public_contracts": [{"task_name": "dev1", "public_contract": table["dev1"]["instruction"]}],
                                  "historical_evidence": rows}}
            raw = json.dumps({"body": "Verify public outputs before finishing.", "resources": {}, "rationale": "Observed development gap."}) if number == 1 else '{"no_change":true,"reason":"No supported residual gap."}'
            outcome = native.generation_outcome(value, raw)
            put(directory / "reserved.json", {"kind": "generation"})
            put(directory / "generation_input.json", value)
            put(directory / "request.json", {**settings, "messages": native.generation_messages(value)})
            put(directory / "completion.json", stream(directory / "response.sse", raw, native))
            put(directory / "outcome.json", outcome)
            put(directory / "generation_status.json", {"status": "generated" if number == 1 else "no_effective_change", "no_retry": True})
            if outcome["candidate"]:
                library[index] = outcome["candidate"]["skill"]
        put(folder / "library.json", library)
        put(folder / "candidate_status.json", {"usable": number == 1, "changed_skills": [chosen] if number == 1 else []})
        if number == 1:
            for name in names[:5]:
                for index in (1, 2):
                    saved_trial(root, native, table, template, settings, f"candidates/001/dev/slots/{name}/{index:02d}",
                                "candidate_1_development", library, chosen if name == "dev1" and index == 1 else None, reward=0)
            current = library
            evidence_sources = evidence_sources + ["candidates/001/dev/slots/dev1/01"]
    put(root / "final/library.json", current)
    put(root / "final/selection.json", {"selected": 1, "rule": "max_official_development_successes_tie_earlier"})
    for name in names:
        for index in range(1, 4):
            saved_trial(root, native, table, template, settings, f"final/slots/{name}/{index:02d}", "final", current,
                        chosen if name == "dev1" and index == 1 else None)
    return root, native


def run(saved_run):
    root, native = saved_run
    return audit.LibraryAudit(root, native=native).run()


def test_complete_native_replay_preserves_unresolved_and_full_observations(saved_run):
    result = run(saved_run)
    assert result["protocol_complete"], result["errors"]
    assert result["reserved_executor_slots"] == 52
    assert result["actual_harbor_trials"] == result["valid_trials"] == 3
    assert result["unresolved_trials"] == 49
    assert result["generation_slots_reserved"] == 2
    assert result["per_task"][0]["old_rewards"] == [1, None, None]
    assert result["per_task"][-1]["regression"] == "uncertain"
    assert "?" in audit.report(result)


@pytest.mark.parametrize("mutation,expected", [
    ("catalog", "catalog_drift"), ("binding", "frozen_bytes_changed"),
    ("raw", "selector_raw_mismatch"), ("source", "generation_source_grouping_drift"),
    ("extra", "unallocated_or_extra_reserved_slot"), ("identity", "duplicate_authoritative_sandbox_id"),
    ("context", "effective_context_or_summarization_drift"), ("final", "final_library_not_selected_candidate"),
])
def test_detects_artifact_drift(saved_run, mutation, expected):
    root, _ = saved_run
    if mutation == "catalog":
        put(root / "baseline/slots/dev1/01/catalog.json", [{"skill_id": "manually_assigned", "description": "wrong"}])
    elif mutation == "binding":
        (root / "frozen/sentinel").write_bytes(b"changed")
    elif mutation == "raw":
        put(root / "baseline/slots/dev1/01/selector_response.json", {"raw": "SKILL: terminalbench.family-p003\nWHY: other"})
    elif mutation == "source":
        path = root / "candidates/001/generation/terminalbench.family-p004/generation_input.json"
        value = audit.read(path)
        value["source_slots"] = ["final/slots/dev1/01"]
        put(path, value)
    elif mutation == "extra":
        put(root / "final/slots/dev1/04/reserved.json", {"kind": "executor"})
    elif mutation == "identity":
        first = audit.read(root / "baseline/slots/dev1/01/jobs/run/dev1__fresh/snapshot.json")
        put(root / "final/slots/dev1/01/jobs/run/dev1__fresh/snapshot.json", first)
    elif mutation == "context":
        path = root / "final/slots/dev1/01/jobs/run/dev1__fresh/agent/effective_agent_config.json"
        value = audit.read(path)
        value["max_input_tokens"] = 8192
        put(path, value)
    else:
        put(root / "final/library.json", audit.read(root / "frozen/seed_library.json"))
    result = run(saved_run)
    assert not result["passed"]
    assert expected in {error["code"] for error in result["errors"]}


def test_regression_no_unknown_as_zero():
    assert audit.regression_status([1, 0, 0], [1, 1, 0]) == "passed"
    assert audit.regression_status([1, 1, 0], [1, 0, 0]) == "failed"
    assert audit.regression_status([1, None, 0], [1, 0, 0]) == "uncertain"
