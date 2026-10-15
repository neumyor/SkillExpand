"""Offline audit of the frozen 2 x 2 loading study; no model or editor calls."""
import argparse
from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


CELLS = ("old_single", "evolved_single", "old_autonomous", "evolved_autonomous")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def numeric_reward(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1)


def aggregate(rows):
    """Unknown outcomes are never imputed as failures or removed from denominators."""
    rewards = [row.get("reward") for row in rows]
    known = [value for value in rewards if numeric_reward(value)]
    successes = sum(value == 1 for value in known)
    return {"slots": len(rows), "valid": len(known), "successes": successes,
            "unresolved": len(rows) - len(known), "complete": bool(rows) and len(known) == len(rows),
            "success_rate": successes / len(rows) if rows and len(known) == len(rows) else None,
            "success_rate_bounds": [successes / len(rows), (successes + len(rows) - len(known)) / len(rows)]
            if rows else None}


def factorial_effects(cells):
    """Report percentage-point differences only with all outcomes observed."""
    summaries = {cell: aggregate(cells.get(cell, [])) for cell in CELLS}
    complete = all(value["complete"] for value in summaries.values())
    equal_counts = len({value["slots"] for value in summaries.values()}) == 1
    rates = {cell: value["success_rate"] for cell, value in summaries.items()}
    result = {"complete": complete and equal_counts, "cells": summaries,
              "evolution_single": None, "evolution_autonomous": None,
              "loading_old": None, "loading_evolved": None, "interaction": None}
    if result["complete"]:
        result.update(evolution_single=rates["evolved_single"] - rates["old_single"],
                      evolution_autonomous=rates["evolved_autonomous"] - rates["old_autonomous"],
                      loading_old=rates["old_autonomous"] - rates["old_single"],
                      loading_evolved=rates["evolved_autonomous"] - rates["evolved_single"])
        result["interaction"] = result["evolution_autonomous"] - result["evolution_single"]
    return result


def measured(metrics):
    metrics = metrics if isinstance(metrics, dict) else {}
    aliases = {"input": ("total_prompt_tokens", "prompt_tokens", "input_tokens", "n_input_tokens"),
               "output": ("total_completion_tokens", "completion_tokens", "output_tokens", "n_output_tokens"),
               "cached": ("total_cached_tokens", "cached_tokens", "n_cache_tokens"), "total": ("total_tokens",)}
    values = {}
    for label, names in aliases.items():
        key = next((key for key in names if isinstance(metrics.get(key), (int, float))
                    and not isinstance(metrics[key], bool)), None)
        values[label] = metrics[key] if key else None
    if values["total"] is None and all(values[key] is not None for key in ("input", "output")):
        values["total"] = values["input"] + values["output"]
    return values


def measurements(rows):
    result = {}
    for label in ("input", "output", "cached", "total", "duration_seconds", "rounds", "tool_calls"):
        values = [row.get(label) if label in ("duration_seconds", "rounds", "tool_calls")
                  else row.get("tokens", {}).get(label) for row in rows]
        known = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
        result[label] = {"observed_sum": sum(known) if known else None, "known_records": len(known),
                         "unknown_records": len(rows) - len(known)}
    return result


def completion_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value.get("raw"), str):
        return value["raw"]
    return value["choices"][0]["message"]["content"]


class NativeReplay:
    def __init__(self, root):
        root = Path(root)
        sys.path.insert(0, str(root / "code"))
        from skillexpand import schema
        from skillexpand.skill_package import entrypoint_content
        from skillexpand.evaluation.selector import SkillSelector, parse_selection, parse_why
        from skillexpand.runtime.progressive import autonomous_catalog_instruction
        self.schema, self.entrypoint = schema, entrypoint_content
        self.selector, self.parse_selection, self.parse_why = SkillSelector, parse_selection, parse_why
        self.autonomous_instruction = autonomous_catalog_instruction
        self.runtime = load_module("_loading_frozen_runtime", root / "runtime.py")
        self.study = load_module("_loading_frozen_study", root / "study.py")
        validator = root / "code/summarize_tb21_empirical.py"
        if not validator.exists():
            validator = root / "code/scripts/summarize_tb21_empirical.py"
        self.validator = load_module("_loading_official_validator", validator)
        self.relay = load_module("_loading_sse", root / "code/skillexpand/runtime/llm_relay.py")

    def skill(self, value):
        return self.schema.from_dict(self.schema.Skill, value)

    def selector_messages(self, instruction, library):
        host = SimpleNamespace(benchmark_name="terminalbench")
        messages = self.selector(host).build_prompt(instruction, [self.skill(item) for item in library])
        return [{"role": "system" if message.type == "system" else "user", "content": message.content}
                for message in messages]

    def stream(self, path):
        return self.relay.coalesce_sse([Path(path).read_bytes()])


def common_agent(agent):
    """Only the predeclared loading mechanism and its manifest path may differ."""
    result = json.loads(json.dumps(agent))
    result["name"] = "terminus-2"
    kwargs = result.setdefault("kwargs", {})
    for key in ("loading_mode", "loading_manifest"):
        kwargs.pop(key, None)
    return result


def resource_invocation_evidence(trace, manifest):
    """Conservative command evidence; terminal observations are reported separately."""
    import re
    rows = []
    resources = {path: info for path, info in manifest.get("files", {}).items()
                 if info["relative_path"] != "SKILL.md"}
    for index, step in enumerate(trace.get("steps", [])):
        calls = step.get("tool_calls") or []
        results = (step.get("observation") or {}).get("results", [])
        for call in calls:
            arguments = call.get("arguments", {})
            command = arguments.get("keystrokes", arguments.get("command", "")) if isinstance(arguments, dict) else ""
            if not isinstance(command, str):
                continue
            for path, info in resources.items():
                pattern = r"(?:^|[\n;&|])\s*(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*)" + (
                    r"(?:(?:python(?:3(?:\.\d+)?)?|bash|sh|node|perl|ruby)\s+(?:-[^\s]+\s+)*)?"
                    + re.escape(path) + r"(?=\s|$|[;&|])")
                if not re.search(pattern, command):
                    continue
                call_id = call.get("tool_call_id", call.get("id"))
                linked = [result for result in results if call_id is not None and
                          result.get("source_call_id", result.get("tool_call_id", result.get("call_id"))) == call_id]
                if not linked and len(calls) == 1 and len(results) == 1:
                    linked = results
                rows.append({"step_index": index, "step_id": step.get("step_id"), "path": path,
                             "skill_id": info["skill_id"], "resource": info["relative_path"],
                             "command": command, "observation_linked": bool(linked),
                             "observed_results": [item.get("content", "") for item in linked],
                             "evidence": "mounted_resource_invocation_with_observation" if linked else
                                         "submitted_invocation_observation_unlinked",
                             "resource_correctness": "unknown"})
    return rows


class LoadingAudit:
    def __init__(self, root, native=None):
        self.root = Path(root).resolve()
        self.manifest = read(self.root / "manifest.json")
        self.remote = Path(self.manifest.get("remote_root", self.root))
        self.phase = self.manifest.get("phase", self.manifest.get("stage", "formal"))
        if self.phase not in ("formal", "smoke"):
            self.phase = self.manifest.get("mode", "formal")
        self.native = native or NativeReplay(self.root)
        self.libraries = {key: read(self.root / "frozen/libraries" / (key + ".json"))
                          for key in ("old", "evolved")}
        self.table = {row["task_name"]: row for row in read(self.root / "frozen/task_table.json")}
        self.panel = read(self.root / "frozen/panel.json")
        self.schedule = read(self.root / "frozen/schedule.json")
        self.template = read(self.root / "frozen/executor_template.json")
        self.selector_settings = read(self.root / "frozen/selector_settings.json")
        self.excluded = set(read(self.root / "frozen/historical_sandbox_ids.json"))
        self.errors, self.pending, self.source_limits = [], [], []
        self.trials, self.identities = [], defaultdict(list)

    def path(self, value):
        path = Path(value)
        if path.is_relative_to(self.remote):
            return self.root / path.relative_to(self.remote)
        return path if path.is_absolute() else self.root / path

    def normalized(self, value):
        if isinstance(value, str) and value.startswith(str(self.remote) + "/"):
            return str(self.path(value))
        if isinstance(value, list):
            return [self.normalized(item) for item in value]
        if isinstance(value, dict):
            return {key: self.normalized(item) for key, item in value.items()}
        return value

    def issue(self, target, path, code):
        path = Path(path)
        item = {"path": str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path), "code": code}
        if item not in target:
            target.append(item)

    def error(self, path, code):
        self.issue(self.errors, path, code)

    def same(self, path, actual, expected, code):
        if self.normalized(actual) != self.normalized(expected):
            self.error(path, code)
            return False
        return True

    def optional(self, path, *, required=False, source=False):
        if not path.exists():
            if required:
                self.issue(self.pending, path, "missing_artifact")
            return None
        try:
            return read(path)
        except (OSError, ValueError, TypeError):
            self.issue(self.source_limits if source else self.errors, path, "unreadable_artifact")
            return None

    def freeze(self):
        bindings = self.optional(self.root / "bindings.json", required=True) or []
        if not bindings:
            self.error(self.root / "bindings.json", "empty_bindings")
        for row in bindings:
            current, snapshot = self.path(row["current"]), self.path(row["snapshot"])
            try:
                if current.resolve() == snapshot.resolve() or current.read_bytes() != snapshot.read_bytes():
                    self.error(current, "frozen_bytes_changed")
            except OSError:
                self.error(current, "frozen_binding_missing")
        ids = set()
        for name, library in self.libraries.items():
            current = [row["skill_id"] for row in library]
            if len(current) != 3 or len(set(current)) != 3:
                self.error(self.root / "frozen/libraries" / (name + ".json"), "invalid_library_topology")
            if ids and set(current) != ids:
                self.error(self.root / "frozen/libraries" / (name + ".json"), "library_topologies_differ")
            ids = set(current)
        routing = lambda library: sorted([{key: row[key] for key in ("skill_id", "description")}
                                          for row in library], key=lambda row: row["skill_id"])
        self.same(self.root / "frozen/libraries/evolved.json", routing(self.libraries["evolved"]),
                  routing(self.libraries["old"]), "routing_catalog_drift_between_libraries")
        prior_root = self.manifest.get("source_prior_root")
        if prior_root:
            for name, relative in (("old", "frozen/seed_library.json"), ("evolved", "final/library.json")):
                source = Path(prior_root) / relative
                if source.exists():
                    if source.read_bytes() != (self.root / "frozen/libraries" / (name + ".json")).read_bytes():
                        self.error(source, "library_not_exact_prior_source_bytes")
                else:
                    self.issue(self.source_limits, source, "prior_source_external_to_archive_not_rechecked")
        slots = [(row.get("cell"), row.get("task"), row.get("attempt")) for row in self.schedule]
        if len(slots) != len(set(slots)):
            self.error(self.root / "frozen/schedule.json", "duplicate_scheduled_slot")
        if self.phase == "formal":
            expected = {(cell, task, attempt) for cell in CELLS for task in self.table for attempt in (1, 2, 3)}
            if len(self.table) != 7 or set(slots) != expected or len(slots) != 84:
                self.error(self.root / "frozen/schedule.json", "formal_factorial_schedule_not_84")
        elif self.phase == "smoke":
            if len(slots) > 8 or any(task not in self.panel["development"] for _, task, _ in slots):
                self.error(self.root / "frozen/schedule.json", "smoke_cap_or_task_boundary_violation")
        else:
            self.error(self.root / "manifest.json", "invalid_phase")
        if any(cell not in CELLS or task not in self.table or not isinstance(attempt, int)
               or isinstance(attempt, bool) or attempt < 1 for cell, task, attempt in slots):
            self.error(self.root / "frozen/schedule.json", "invalid_scheduled_identity")
        kwargs = self.template["agents"][0].get("kwargs", {})
        self.same(self.root / "frozen/executor_template.json", kwargs.get("model_info"),
                  {"max_input_tokens": 262144, "max_output_tokens": 65536}, "context_drift")
        if (self.root / "integrity_abort.json").exists():
            self.error(self.root / "integrity_abort.json", "integrity_abort")
        return len(bindings)

    def expected_manifest(self, library, mode, selected=None):
        catalog = [{"skill_id": row["skill_id"], "description": row["description"]}
                   for row in sorted(library, key=lambda row: row["skill_id"])]
        mounted = library if mode == "autonomous" else [row for row in library if row["skill_id"] == selected]
        files = {}
        for row in mounted:
            skill = self.native.skill(row)
            contents = {"SKILL.md": self.native.entrypoint(skill, package=skill.package_mode)}
            if skill.package_mode:
                contents.update(skill.resources)
            for relative, content in contents.items():
                path = f"/opt/openclaw-skills/{skill.skill_id}/{relative}"
                files[path] = {"skill_id": skill.skill_id, "relative_path": relative, "content": content}
        suffix = self.native.autonomous_instruction([self.native.skill(row) for row in library]) if mode == "autonomous" else (
            f"\n\nPlease read the skill first. Read /opt/openclaw-skills/{selected}/SKILL.md before executing task commands.")
        return {"mode": mode, "files": files, "skill_ids": [row["skill_id"] for row in mounted],
                "catalog": catalog, "initial_suffix": suffix,
                "load_stage": "after_selection" if mode == "single" else "executor_autonomous"}

    def selector(self, slot, task, library):
        request = self.optional(slot / "selector_request.json", required=True)
        self.same(slot / "selector_request.json", request,
                  {**self.selector_settings, "messages": self.native.selector_messages(task["instruction"], library)},
                  "selector_request_drift")
        response = self.optional(slot / "selector_response.json", required=True)
        selection = self.optional(slot / "selection.json", required=True)
        if response is None or selection is None:
            return None
        try:
            raw = completion_text(response)
            if response.get("status") != "failed":
                self.same(slot / "selector_response.sse", completion_text(self.native.stream(slot / "selector_response.sse")),
                          raw, "selector_stream_differs_from_response")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self.error(slot, "selector_stream_missing_or_invalid")
            return None
        self.same(slot / "selection.json", selection.get("raw"), raw, "selector_raw_mismatch")
        catalog = [{"skill_id": row["skill_id"], "description": row["description"]}
                   for row in sorted(library, key=lambda row: row["skill_id"])]
        self.same(slot / "selection.json", selection.get("catalog"), catalog, "selector_catalog_mismatch")
        chosen = self.native.parse_selection(raw, [row["skill_id"] for row in library])
        if chosen:
            skill = next(row for row in library if row["skill_id"] == chosen)
            for key, value in {"skill_id": chosen, "loaded_skill_id": chosen,
                               "loaded_skill_key": f"{chosen}@v{skill['version']}", "ok": True,
                               "load_stage": "after_selection", "why": self.native.parse_why(raw)}.items():
                self.same(slot / "selection.json", selection.get(key), value, "native_selection_replay_mismatch")
        elif selection.get("ok") is not False:
            self.error(slot / "selection.json", "invalid_selector_given_fallback")
        return chosen

    def mount(self, slot, expected):
        manifest = self.optional(slot / "load_manifest.json", required=True)
        self.same(slot / "load_manifest.json", manifest, expected, "mounted_manifest_drift")
        source = slot / "skills"
        files = {"/opt/openclaw-skills/" + path.relative_to(source).as_posix(): path.read_bytes()
                 for path in source.rglob("*") if path.is_file()}
        if any(path.is_symlink() for path in source.rglob("*")):
            self.error(source, "mounted_symlink")
        expected_bytes = {path: info["content"].encode("utf-8") for path, info in expected["files"].items()}
        if files != expected_bytes:
            self.error(source, "mounted_package_bytes_or_extra_files")
        return manifest

    def loading(self, agent, manifest, mode, trace, row, valid):
        visibility = self.optional(agent / "loading_visibility.json", source=not valid, required=valid)
        events = self.optional(agent / "loading_access_events.json", source=not valid, required=valid)
        observer = self.optional(agent / "loading_observer.json", source=not valid, required=valid)
        if not visibility or events is None or observer is None:
            self.issue(self.source_limits, agent, "loading_observation_unavailable")
            return
        expected_visibility = {"mode": mode, "catalog": manifest["catalog"], "initial_suffix": manifest["initial_suffix"],
                               "bodies_in_initial_prompt": False, "resource_contents_in_initial_prompt": False}
        for key, value in expected_visibility.items():
            self.same(agent / "loading_visibility.json", visibility.get(key), value, "initial_visibility_drift")
        self.same(agent / "loading_visibility.json", visibility.get("observer_ready"), observer, "observer_ready_mismatch")
        if observer.get("watched_files") != len(manifest["files"]) or not isinstance(observer.get("pid"), int):
            self.error(agent / "loading_observer.json", "observer_watch_set_incomplete")
        if not isinstance(events, list):
            self.error(agent / "loading_access_events.json", "invalid_access_events")
            return
        for event in events:
            if event.get("kind") == "overflow":
                self.issue(self.source_limits, agent / "loading_access_events.json", "inotify_overflow_loading_unknown")
            elif event.get("path") not in manifest["files"]:
                self.error(agent / "loading_access_events.json", "access_outside_frozen_manifest")
            if event.get("kind") == "access" and not (event.get("mask", 0) & 1):
                self.error(agent / "loading_access_events.json", "access_without_kernel_access_mask")
            if not isinstance(event.get("at"), (int, float)):
                self.error(agent / "loading_access_events.json", "access_timestamp_missing")
            elif isinstance(visibility.get("at"), (int, float)) and event["at"] < visibility["at"]:
                self.error(agent / "loading_access_events.json", "access_precedes_executor_visibility")
        evidence = self.native.study.collect_loading_evidence(trace or {"steps": []}, events, manifest)
        body_reads = [event for event in evidence["actual_file_accesses"] if event["resource"] == "SKILL.md"]
        resource_reads = [event for event in evidence["actual_file_accesses"] if event["resource"] != "SKILL.md"]
        row["loading"] = {**evidence, "body_read_skill_ids": sorted({event["skill_id"] for event in body_reads}),
                          "resource_read_skill_ids": sorted({event["skill_id"] for event in resource_reads}),
                          "resource_invocations": resource_invocation_evidence(trace or {}, manifest),
                          "visibility_timestamp": visibility.get("at")}
        if visibility.get("at") is None:
            self.error(agent / "loading_visibility.json", "visibility_timestamp_missing")
        observations = agent / "terminal_observations.jsonl"
        if observations.exists():
            try:
                snapshots = [json.loads(line) for line in observations.read_text().splitlines() if line.strip()]
                row["loading"]["terminal_observations"] = len(snapshots)
                row["loading"]["temporal_access_evidence"] = [
                    {"path": event["path"], "at": event["at"],
                     "following_observation_at": next((item["at"] for item in snapshots
                         if isinstance(item.get("at"), (int, float)) and item["at"] >= event["at"]), None)}
                    for event in events if event.get("kind") == "access" and isinstance(event.get("at"), (int, float))]
            except (OSError, ValueError, KeyError, TypeError):
                self.issue(self.source_limits, observations, "terminal_observation_timestamps_unreadable")
        else:
            self.issue(self.source_limits, observations, "terminal_observation_timestamps_missing")

    def initial_prompt(self, agent, manifest, library, row, valid):
        path = agent / "raw_model_requests.jsonl"
        if not path.exists():
            self.issue(self.pending if valid else self.source_limits, path, "initial_model_request_missing")
            return
        try:
            requests = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if not requests:
                raise ValueError("empty requests")
            messages = requests[0]["messages"]
            text = "\n".join(message.get("content", "") for message in messages if isinstance(message.get("content"), str))
            if manifest["initial_suffix"] not in text:
                self.error(path, "initial_model_request_missing_exact_loading_instruction")
            leaked = []
            for skill in library:
                if skill["body"].strip() and skill["body"].strip() in text:
                    leaked.append(skill["skill_id"] + ":body")
                leaked.extend(skill["skill_id"] + ":" + name for name, content in skill.get("resources", {}).items()
                              if content.strip() and content.strip() in text)
            if leaked:
                self.error(path, "skill_contents_in_initial_model_request")
            row["initial_prompt"] = {"request_count": len(requests), "loading_suffix_observed": manifest["initial_suffix"] in text,
                                     "full_body_or_resource_leaks": leaked}
            for request in requests:
                expected = {"model": self.template["agents"][0]["model_name"].removeprefix("openai/"),
                            "max_tokens": 65536, "top_p": .95, "top_k": 20, "enable_thinking": True}
                for key, value in expected.items():
                    self.same(path, request.get(key), value, "actual_provider_parameters_drift")
                self.same(path, request.get("chat_template_kwargs", {}).get("preserve_thinking"), True,
                          "actual_thinking_preservation_drift")
        except (OSError, ValueError, TypeError, KeyError):
            self.issue(self.errors if valid else self.source_limits, path, "raw_model_requests_unreadable")

    def trial(self, scheduled):
        cell, task_name, attempt = scheduled["cell"], scheduled["task"], scheduled["attempt"]
        slot = self.root / self.phase / "slots" / cell / task_name / f"{attempt:02d}"
        start_errors, start_pending = len(self.errors), len(self.pending)
        library_name, mode = cell.rsplit("_", 1)
        library, task = self.libraries[library_name], self.table[task_name]
        row = {"path": str(slot.relative_to(self.root)), "cell": cell, "task": task_name, "attempt": attempt,
               "library": library_name, "mode": mode, "reserved": (slot / "reserved.json").exists(),
               "finished": False, "valid": False, "reward": None, "tokens": measured(None)}
        self.trials.append(row)
        trial_dirs = [path for path in (slot / "jobs").glob("*/*") if path.is_dir() and "__" in path.name]
        row["actual_harbor_trials"] = len(trial_dirs)
        if len(trial_dirs) > 1:
            self.error(slot, "multiple_harbor_trials_or_retry")
        if not row["reserved"]:
            self.issue(self.pending, slot, "slot_not_reserved")
            return row
        record = self.optional(slot / "record.json", required=True)
        if record is None:
            return row
        row.update(finished=True, error_type=record.get("error_type"), error=record.get("error"),
                   duration_seconds=record.get("duration_seconds"))
        if record.get("status") not in ("valid", "unresolved") or record.get("no_retry") is not True:
            self.error(slot / "record.json", "slot_status_or_retry_invalid")
        if record.get("status") == "unresolved" and record.get("reward") is not None:
            self.error(slot / "record.json", "unresolved_reward_not_null")
        self.same(slot / "input.json", self.optional(slot / "input.json", required=True),
                  {"task": task_name, "cell": cell, "attempt": attempt, "library": library, "mode": mode}, "slot_input_drift")
        catalog = [{"skill_id": item["skill_id"], "description": item["description"]}
                   for item in sorted(library, key=lambda item: item["skill_id"])]
        self.same(slot / "catalog.json", self.optional(slot / "catalog.json", required=True), catalog, "catalog_drift")
        selected = self.selector(slot, task, library) if mode == "single" else None
        if mode == "autonomous":
            for name in ("selector_request.json", "selector_response.json", "selector_response.sse", "selection.json"):
                if (slot / name).exists():
                    self.error(slot / name, "autonomous_external_selector_or_fixed_injection")
        row["selected_skill_id"] = selected
        can_load = mode == "autonomous" or selected is not None
        manifest = self.mount(slot, self.expected_manifest(library, mode, selected)) if can_load else None
        config = self.optional(slot / "harbor_config.json", required=can_load)
        if config:
            expected_config = self.native.runtime.build_config(self.template, self.root, slot, task_name, slot / "skills", mode)
            self.same(slot / "harbor_config.json", config, expected_config, "full_harbor_configuration_drift")
            row["common_agent"] = common_agent(config["agents"][0])
            for path in (slot / "jobs").glob("*/config.json"):
                self.same(path, self.optional(path, source=True), config, "actual_harbor_job_config_drift")
        result_path = self.path(record["result_path"]) if record.get("result_path") else None
        if result_path and (not result_path.is_relative_to(slot) or result_path.parent not in trial_dirs):
            self.error(slot / "record.json", "result_outside_reserved_trial")
            result_path = None
        trace = None
        if result_path:
            snapshot = self.optional(result_path.parent / "snapshot.json", source=True)
            identity = (snapshot or {}).get("source_sandbox_id")
            row["sandbox_id"] = identity
            if identity:
                self.identities[identity].append(row)
                self.same(slot / "record.json", record.get("sandbox_id"), identity, "snapshot_identity_mismatch")
                if identity in self.excluded:
                    self.error(slot, "historical_sandbox_reused")
            elif record.get("status") == "valid":
                self.error(slot, "valid_without_authoritative_identity")
            result = self.optional(result_path, source=True)
            if result and config:
                official = result.get("config", {})
                self.same(result_path, {**official.get("agent", {}), "name": config["agents"][0].get("name")},
                          config["agents"][0], "official_agent_configuration_drift")
                self.same(result_path, official.get("environment"), config["environment"], "official_environment_drift")
                self.same(result_path, official.get("verifier"), config["verifier"], "official_verifier_drift")
                self.same(result_path, official.get("task", {}).get("path"), str(self.root / "frozen/tasks" / task_name),
                          "official_task_path_drift")
            agent = result_path.parent / "agent"
            trace = self.optional(agent / "trajectory.json", source=True)
            if trace:
                row.update(tokens=measured(trace.get("final_metrics")), rounds=len(trace.get("steps", [])),
                           tool_calls=sum(len(step.get("tool_calls") or []) for step in trace.get("steps", [])))
            try:
                _, reward, trajectory = self.native.validator.validate_trial(result_path, task_name, activation=False,
                    model=self.template["agents"][0]["model_name"])
                if record.get("status") == "valid":
                    if not numeric_reward(record.get("reward")) or record["reward"] != reward:
                        self.error(slot / "record.json", "reward_not_official_numeric_binary")
                    self.same(slot / "record.json", record.get("trajectory_path"), trajectory, "trajectory_path_mismatch")
                    if not can_load:
                        self.error(slot / "record.json", "valid_without_loading_policy")
                    row.update(valid=True, reward=reward)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row["official_issue"] = str(exc)
                if record.get("status") == "valid":
                    self.error(slot / "record.json", "claimed_valid_failed_official_validation")
            effective = self.optional(agent / "effective_agent_config.json", source=not row["valid"], required=row["valid"])
            if effective:
                expected = {"max_input_tokens": 262144, "max_output_tokens": 65536, "enable_summarize": True,
                            "proactive_summarization_threshold": 8000, "loading_mode": mode,
                            "api_base": self.template["agents"][0]["kwargs"]["api_base"]}
                for key, value in expected.items():
                    self.same(agent / "effective_agent_config.json", effective.get(key), value, "effective_runtime_drift")
            if manifest:
                self.loading(agent, manifest, mode, trace, row, row["valid"])
                self.initial_prompt(agent, manifest, library, row, row["valid"])
                evidence_path = slot / "loading_evidence.json"
                if evidence_path.exists() and trace:
                    events = self.optional(agent / "loading_access_events.json", source=True)
                    if events is not None:
                        self.same(evidence_path, read(evidence_path),
                                  self.native.study.collect_loading_evidence(trace, events, manifest), "loading_evidence_replay_drift")
        elif record.get("status") == "valid":
            self.error(slot / "record.json", "valid_without_official_result")
        if len(self.errors) > start_errors or len(self.pending) > start_pending:
            row.update(valid=False, reward=None, audit_rejected=True)
        return row

    def run(self):
        bindings = self.freeze()
        for scheduled in self.schedule:
            self.trial(scheduled)
        for identity, rows in self.identities.items():
            if len(rows) > 1:
                for row in rows:
                    self.error(self.root / row["path"], "duplicate_sandbox_identity")
                    row.update(valid=False, reward=None, audit_rejected=True)
        allowed = {self.root / row["path"] for row in self.trials}
        extra = []
        for path in (self.root / self.phase / "slots").rglob("reserved.json"):
            if path.parent not in allowed:
                self.error(path, "unscheduled_reserved_slot")
                extra.append(path)
        calls = [path for path in (self.root / self.phase / "slots").rglob("generation_input.json")]
        if calls:
            self.error(calls[0], "unexpected_generation_in_fixed_library_study")
        common = [row["common_agent"] for row in self.trials if row.get("common_agent")]
        if common and any(value != common[0] for value in common[1:]):
            self.error(self.root, "nonmechanism_executor_configuration_differs_between_cells")
        by_cell = {cell: [row for row in self.trials if row["cell"] == cell] for cell in CELLS}
        per_task = []
        for task in self.table:
            cell_rows = {cell: [row for row in by_cell[cell] if row["task"] == task] for cell in CELLS}
            per_task.append({"task": task, "split": self.table[task].get("split"),
                             "rewards": {cell: [row["reward"] for row in sorted(rows, key=lambda row: row["attempt"])]
                                         for cell, rows in cell_rows.items()}, "effects": factorial_effects(cell_rows)})
        loading_summary = {}
        for cell, rows in by_cell.items():
            available = [row["loading"] for row in rows if row.get("loading")]
            loading_summary[cell] = {"observed_trials": len(available), "unknown_trials": len(rows) - len(available),
                "body_read_set_frequencies": dict(Counter("|".join(item["body_read_skill_ids"]) or "none"
                                                          for item in available)),
                "multiple_bodies_read_trials": sum(len(item["body_read_skill_ids"]) > 1 for item in available),
                "mounted_resource_invocations": sum(len(item["resource_invocations"]) for item in available),
                "invocations_with_linked_observation": sum(sum(call["observation_linked"] for call in item["resource_invocations"])
                                                           for item in available)}
        return {"passed": not self.errors, "protocol_complete": not self.errors and not self.pending,
                "phase": self.phase, "frozen_binding_count": bindings, "errors": self.errors,
                "pending": self.pending, "source_limits": self.source_limits,
                "planned_slots": len(self.schedule), "reserved_slots": sum(row["reserved"] for row in self.trials) + len(extra),
                "actual_harbor_trials": sum(row["actual_harbor_trials"] for row in self.trials),
                "finished_slots": sum(row["finished"] for row in self.trials),
                "overall": aggregate(self.trials), "factorial": factorial_effects(by_cell),
                "by_split": {split: factorial_effects({cell: [row for row in rows if self.table[row["task"]].get("split") == split]
                                                       for cell, rows in by_cell.items()})
                             for split in ("development", "regression")},
                "per_task": per_task, "loading_summary": loading_summary, "measurements": measurements(self.trials),
                "trials": [{key: value for key, value in row.items() if key != "common_agent"} for row in self.trials],
                "limitations": ["Closed-set tasks and existing evolved library; no new evolution or unseen-task claim.",
                    "Kernel file ACCESS proves access, not model consumption or semantic use.",
                    "Reading multiple Skills gives a joint trajectory; no per-Skill success attribution.",
                    "Resource invocation and its observed output do not establish resource correctness or causal benefit.",
                    "Unresolved outcomes stay null; effects require all compared outcomes valid.",
                    "Context 262144 and common runtime are controlled conditions, not evolution benefit.",
                    "Trial duration sums reflect concurrent work, not elapsed wall time; monetary cost is unknown."]}


def report(result):
    reward_text = lambda values: " ".join("?" if value is None else str(value) for value in values) or "not scheduled"
    lines = ["# Loading factorial results", "", f"Phase: {result['phase']}; audit passed: {result['passed']}; "
             f"protocol complete: {result['protocol_complete']}.",
             f"Reserved {result['reserved_slots']}/{result['planned_slots']}; actual Harbor trials {result['actual_harbor_trials']}; "
             f"finished {result['finished_slots']}; valid {result['overall']['valid']}; "
             f"successes {result['overall']['successes']}; unresolved or pending {result['overall']['unresolved']}.", "",
             "| Task | Split | Old single | Evolved single | Old autonomous | Evolved autonomous |",
             "|---|---|---|---|---|---|"]
    for row in result["per_task"]:
        lines.append("| " + " | ".join([row["task"], str(row["split"])] + [reward_text(row["rewards"][cell]) for cell in CELLS]) + " |")
    lines.extend(["", "`?` remains unresolved or pending, never reward zero.", "", "## Factorial effects", "",
                  "Differences are fractions of success probability; multiply by 100 for percentage points.",
                  "```json", json.dumps(result["factorial"], indent=2), "```", "", "## Loading evidence", "",
                  "```json", json.dumps(result["loading_summary"], indent=2), "```", "", "## Measurements", "",
                  "```json", json.dumps(result["measurements"], indent=2), "```", "", "## Limitations", ""])
    lines += ["- " + item for item in result["limitations"]]
    lines += ["", "## Audit issues", "", "```json", json.dumps({key: result[key] for key in ("errors", "pending", "source_limits")}, indent=2), "```", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = LoadingAudit(args.root).run()
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.report:
        args.report.write_text(report(result), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("passed", "protocol_complete", "phase", "reserved_slots",
                                                 "actual_harbor_trials", "overall", "errors", "pending")}))
    return 0 if result["protocol_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
