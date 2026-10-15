"""Saved synthetic artifacts test strict auditing without remote/model calls."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1]
REPO = SOURCE.parents[1]
spec = importlib.util.spec_from_file_location("factorial_audit_tested", SOURCE / "audit.py")
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def stream(path, text, native):
    path.write_text("data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]})
                    + "\n\ndata: [DONE]\n\n")
    return native.stream(path)


@pytest.fixture
def saved(tmp_path):
    root = tmp_path / "smoke"
    (root / "code").mkdir(parents=True)
    (root / "code/skillexpand").symlink_to(REPO / "src/skillexpand", target_is_directory=True)
    shutil.copy(REPO / "scripts/summarize_tb21_empirical.py", root / "code/summarize_tb21_empirical.py")
    for name in ("runtime.py", "study.py"):
        shutil.copy(SOURCE / name, root / name)
    native = audit.NativeReplay(root)
    libraries = {name: [{"skill_id": f"skill-{index}", "family_id": f"family-{index}", "name": f"Skill {index}",
                        "description": f"Operation {index}", "version": 1 if name == "old" else 2,
                        "body": "Read public inputs carefully.\nVerify exact output schema and types.\nCheck observable behavior before finishing.",
                        "resources": {"check.py": "print('public check')\n"} if index == 1 else {},
                        "package_mode": index == 1, "provenance": {}} for index in (1, 2, 3)]
                 for name in ("old", "evolved")}
    task = {"task_name": "dev", "instruction": "Produce the public output.", "split": "development"}
    template = {"agents": [{"name": None, "model_name": "openai/qwen3.6-flash-distill",
        "import_path": "loading_factorial_runtime:LoadingTerminus", "kwargs": {"api_base": "synthetic",
        "model_info": {"max_input_tokens": 262144, "max_output_tokens": 65536}}}],
        "environment": {"import_path": "loading_factorial_runtime:LoadingEnvironment", "kwargs": {}},
        "datasets": [{"path": "placeholder", "task_names": []}], "n_attempts": 1,
        "orchestrator": {"n_concurrent_trials": 1, "retry": {"max_retries": 0}}, "verifier": {"disable": False}}
    settings = {"model": "synthetic", "stream": True}
    schedule = [{"cell": cell, "task": "dev", "attempt": 1} for cell in audit.CELLS]
    put(root / "manifest.json", {"stage": "smoke", "remote_root": str(root), "generation_max": 0})
    put(root / "frozen/libraries/old.json", libraries["old"])
    put(root / "frozen/libraries/evolved.json", libraries["evolved"])
    put(root / "frozen/task_table.json", [task])
    put(root / "frozen/panel.json", {"development": ["dev"], "regression": []})
    put(root / "frozen/schedule.json", schedule)
    put(root / "frozen/executor_template.json", template)
    put(root / "frozen/selector_settings.json", settings)
    put(root / "frozen/historical_sandbox_ids.json", ["old-sandbox"])
    (root / "sentinel").write_bytes(b"frozen\r\n")
    (root / "snapshot").write_bytes(b"frozen\r\n")
    put(root / "bindings.json", [{"current": "sentinel", "snapshot": "snapshot"}])
    inspector = audit.LoadingAudit(root, native=native)
    for index, scheduled in enumerate(schedule):
        cell = scheduled["cell"]
        libname, mode = cell.rsplit("_", 1)
        library = libraries[libname]
        slot = root / "smoke/slots" / cell / "dev/01"
        slot.mkdir(parents=True)
        put(slot / "reserved.json", {"kind": "executor", "no_retry": True, **scheduled})
        put(slot / "input.json", {"task": "dev", "library": library, **scheduled, "mode": mode})
        catalog = [{"skill_id": item["skill_id"], "description": item["description"]} for item in library]
        put(slot / "catalog.json", catalog)
        chosen = "skill-1" if mode == "single" else None
        if chosen:
            raw = "SKILL: skill-1\nWHY: operation match"
            response = stream(slot / "selector_response.sse", raw, native)
            put(slot / "selector_response.json", {**response, "raw": raw})
            put(slot / "selector_request.json", {**settings, "messages": native.selector_messages(task["instruction"], library)})
            put(slot / "selection.json", {"catalog": catalog, "raw": raw, "why": "operation match", "ok": True,
                "skill_id": chosen, "loaded_skill_id": chosen, "loaded_skill_key": f"skill-1@v{library[0]['version']}", "load_stage": "after_selection"})
        manifest = inspector.expected_manifest(library, mode, chosen)
        put(slot / "load_manifest.json", manifest)
        for remote, info in manifest["files"].items():
            path = slot / "skills" / remote.removeprefix("/opt/openclaw-skills/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(info["content"].encode())
        config = native.runtime.build_config(template, root, slot, "dev", slot / "skills", mode)
        put(slot / "harbor_config.json", config)
        trial = slot / "jobs/run/dev__fresh"
        agent = trial / "agent"
        command = "cat /opt/openclaw-skills/skill-1/SKILL.md\n"
        content = manifest["files"]["/opt/openclaw-skills/skill-1/SKILL.md"]["content"]
        trace = {"final_metrics": {"total_prompt_tokens": 20, "total_completion_tokens": 4},
                 "steps": [{"step_id": 1, "timestamp": 12, "tool_calls": [{"tool_call_id": "a", "function_name": "bash_command",
                    "arguments": {"keystrokes": command}}], "observation": {"results": [{"content": content}]}}]}
        events = [{"kind": "access", "path": "/opt/openclaw-skills/skill-1/SKILL.md", "at": 11, "mask": 1}]
        if mode == "autonomous":
            events.append({"kind": "access", "path": "/opt/openclaw-skills/skill-2/SKILL.md", "at": 12, "mask": 1})
        put(agent / "trajectory.json", trace)
        (agent / "raw_model_requests.jsonl").write_text(json.dumps({"model": "qwen3.6-flash-distill",
            "messages": [{"role": "user", "content": task["instruction"] + manifest["initial_suffix"]}],
            "max_tokens": 65536, "top_p": .95, "top_k": 20, "enable_thinking": True,
            "chat_template_kwargs": {"preserve_thinking": True}}) + "\n")
        put(agent / "loading_access_events.json", events)
        ready = {"pid": 20, "watched_files": len(manifest["files"])}
        put(agent / "loading_observer.json", ready)
        put(agent / "loading_visibility.json", {"at": 10, "mode": mode, "catalog": catalog,
            "initial_suffix": manifest["initial_suffix"], "bodies_in_initial_prompt": False,
            "resource_contents_in_initial_prompt": False, "observer_ready": ready})
        (agent / "terminal_observations.jsonl").write_text(json.dumps({"at": 13, "output": content}) + "\n")
        put(slot / "loading_evidence.json", native.study.collect_loading_evidence(trace, events, manifest))
        put(agent / "effective_agent_config.json", {"max_input_tokens": 262144, "max_output_tokens": 65536,
            "enable_summarize": True, "proactive_summarization_threshold": 8000, "api_base": "synthetic", "loading_mode": mode})
        identity = "fresh-" + cell
        put(trial / "snapshot.json", {"source_sandbox_id": identity})
        reward = 0 if index == 0 else 1
        put(trial / "result.json", {"task_name": "dev", "exception_info": None,
            "verifier_result": {"rewards": {"reward": reward}}, "config": {"agent": {**config["agents"][0], "name": "terminus-2"},
                "environment": config["environment"], "verifier": config["verifier"], "task": {"path": str(root / "frozen/tasks/dev")}}})
        (trial / "verifier").mkdir()
        (trial / "verifier/reward.txt").write_text(str(reward))
        (trial / "verifier/test-stdout.txt").write_text("test session starts\n1 passed" if reward else "test session starts\n1 failed")
        put(slot / "record.json", {"status": "valid", "reward": reward, "no_retry": True, "sandbox_id": identity,
            "result_path": str(trial / "result.json"), "trajectory_path": str(agent / "trajectory.json")})
    return root, native


def run(saved):
    return audit.LoadingAudit(saved[0], native=saved[1]).run()


def test_official_zero_preserved_and_true_multiaccess(saved):
    result = run(saved)
    assert result["protocol_complete"], result["errors"]
    assert result["overall"]["valid"] == 4
    assert result["overall"]["successes"] == 3
    assert result["per_task"][0]["rewards"]["old_single"] == [0]
    assert result["loading_summary"]["old_autonomous"]["multiple_bodies_read_trials"] == 1
    assert result["factorial"]["interaction"] == -1


@pytest.mark.parametrize("mutation,expected", [
    ("freeze", "frozen_bytes_changed"), ("context", "effective_runtime_drift"),
    ("fallback", "autonomous_external_selector_or_fixed_injection"),
    ("access", "access_without_kernel_access_mask"), ("mount", "mounted_package_bytes_or_extra_files"),
    ("identity", "duplicate_sandbox_identity"), ("extra", "unscheduled_reserved_slot"),
    ("schedule", "smoke_cap_or_task_boundary_violation"), ("reward", "reward_not_official_numeric_binary")])
def test_artifact_drift_rejected(saved, mutation, expected):
    root, _ = saved
    slot = root / "smoke/slots/old_autonomous/dev/01"
    agent = slot / "jobs/run/dev__fresh/agent"
    if mutation == "freeze":
        (root / "sentinel").write_bytes(b"changed")
    elif mutation == "context":
        path = agent / "effective_agent_config.json"
        value = audit.read(path); value["max_input_tokens"] = 100; put(path, value)
    elif mutation == "fallback":
        put(slot / "selection.json", {"skill_id": "skill-1"})
    elif mutation == "access":
        path = agent / "loading_access_events.json"
        value = audit.read(path); value[0]["mask"] = 32; put(path, value)
    elif mutation == "mount":
        (slot / "skills/skill-1/SKILL.md").write_bytes(b"extra\r\n")
    elif mutation == "identity":
        path = slot / "jobs/run/dev__fresh/snapshot.json"
        put(path, {"source_sandbox_id": "fresh-old_single"})
    elif mutation == "extra":
        put(root / "smoke/slots/old_single/dev/02/reserved.json", {})
    elif mutation == "schedule":
        path = root / "frozen/panel.json"; put(path, {"development": [], "regression": ["dev"]})
    else:
        path = slot / "record.json"; value = audit.read(path); value["reward"] = True; put(path, value)
    result = run(saved)
    assert not result["passed"]
    assert expected in {item["code"] for item in result["errors"]}


def test_unresolved_remains_null_and_effect_unknown(saved):
    root, _ = saved
    path = root / "smoke/slots/evolved_single/dev/01/record.json"
    value = audit.read(path); value.update(status="unresolved", reward=None, error_type="TimeoutException"); put(path, value)
    result = run(saved)
    assert result["passed"]
    assert result["overall"]["valid"] == 3
    assert result["factorial"]["interaction"] is None
    assert result["overall"]["success_rate"] is None
    assert "?" in audit.report(result)


def test_mounted_resource_invocation_excludes_local_basename_and_path_echo():
    mounted = "/opt/openclaw-skills/s/check.py"
    manifest = {"files": {mounted: {"relative_path": "check.py", "skill_id": "s"}}}
    calls = ["cat " + mounted + "\n", "python3 /app/check.py\n", "echo python3 " + mounted + "\n", "python3 " + mounted + "\n"]
    trace = {"steps": [{"step_id": index, "tool_calls": [{"arguments": {"keystrokes": command}}],
                        "observation": {"results": [{"content": "observed output"}]}} for index, command in enumerate(calls)]}
    rows = audit.resource_invocation_evidence(trace, manifest)
    assert len(rows) == 1 and rows[0]["step_index"] == 3 and rows[0]["observation_linked"]


def test_unknown_and_unequal_cell_counts_never_create_effect():
    cells = {cell: [{"reward": 1}] for cell in audit.CELLS}
    cells["old_single"] += [{"reward": 0}]
    assert audit.factorial_effects(cells)["interaction"] is None
    cells["old_single"] = [{"reward": None}]
    assert audit.factorial_effects(cells)["evolution_single"] is None
