"""Independent saved-artifact audit and report; never call a model or evaluator."""
import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def without_creation_time(value):
    if isinstance(value, dict):
        return {key: ({k: without_creation_time(v) for k, v in item.items() if k != "created_at"}
                      if key == "provenance" and isinstance(item, dict) else without_creation_time(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [without_creation_time(item) for item in value]
    return value


def completion_text(value):
    if isinstance(value, str):
        return value
    for key in ("text", "content", "raw", "completion"):
        if isinstance(value.get(key), str):
            return value[key]
    choices = value.get("choices") or []
    if choices:
        return choices[0].get("message", {}).get("content", "")
    raise ValueError("completion_has_no_text")


def measured(metrics):
    metrics = metrics if isinstance(metrics, dict) else {}
    aliases = {"input": ("total_prompt_tokens", "prompt_tokens", "input_tokens"),
               "output": ("total_completion_tokens", "completion_tokens", "output_tokens"),
               "cached": ("total_cached_tokens", "cached_tokens"), "total": ("total_tokens",)}
    result = {}
    for key, names in aliases.items():
        source = next((name for name in names if isinstance(metrics.get(name), (int, float))
                       and not isinstance(metrics[name], bool)), None)
        result[key] = metrics[source] if source else None
    if result["total"] is None and all(result[key] is not None for key in ("input", "output")):
        result["total"] = result["input"] + result["output"]
    return result


def measurements(rows):
    output = {}
    for key in ("input", "output", "cached", "total", "duration_seconds", "rounds", "tool_calls"):
        values = [row.get(key) if key in ("duration_seconds", "rounds", "tool_calls")
                  else row.get("tokens", {}).get(key) for row in rows]
        known = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
        output[key] = {"observed_sum": sum(known) if known else None,
                       "known_records": len(known), "unknown_records": len(rows) - len(known)}
    return output


def regression_status(old, new):
    if not old or not new or any(value is None for value in old + new):
        return "uncertain"
    return "passed" if sum(new) >= sum(old) else "failed"


@dataclass
class Message:
    content: str
    type: str


class NativeReplay:
    """Use frozen native parsing, prompts, package editor and official validator."""
    def __init__(self, root):
        self.root = Path(root)
        sys.path.insert(0, str(self.root / "code"))
        from skillexpand import schema
        from skillexpand.skill_package import entrypoint_content
        from skillexpand.l1.family_discovery import _extract_json
        from skillexpand.evaluation.selector import SkillSelector, parse_selection, parse_why
        self.schema, self.entrypoint = schema, entrypoint_content
        self.selector, self.parse_selection, self.parse_why = SkillSelector, parse_selection, parse_why
        validator_path = self.root / "code/summarize_tb21_empirical.py"
        if not validator_path.exists():
            validator_path = self.root / "code/scripts/summarize_tb21_empirical.py"
        self.validator = load_module("_library_official_validator", validator_path)
        self.runtime = load_module("_library_frozen_runtime", self.root / "runtime.py")
        self.relay = load_module("_library_frozen_sse", self.root / "code/skillexpand/runtime/llm_relay.py")
        filter_path = self.root / "input_filter.py"
        if not filter_path.exists():
            filter_path = self.root / "code/input_filter.py"
        self.filter = load_module("_library_evidence_filter", filter_path)
        path = self.root / "code/skillexpand/l2/editor.py"
        nodes = []
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {
                    "EXISTING_RULE_CHECK", "REASON_PROPOSED", "REASON_NO_OPERATIONS", "REASON_NO_CHANGE"}
                    for target in node.targets):
                nodes.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "EditOutcome":
                nodes.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "SkillEditor":
                node.body = [method for method in node.body if isinstance(method, ast.FunctionDef)
                             and method.name in {"__init__", "build_package_prompt", "propose_package"}]
                nodes.append(node)
        namespace = {"json": json, "dataclass": dataclass, "S": schema, "_extract_json": _extract_json,
                     "SystemMessage": lambda content: Message(content, "system"),
                     "HumanMessage": lambda content: Message(content, "human")}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
        self.editor = namespace["SkillEditor"]

    def skill(self, value):
        return self.schema.from_dict(self.schema.Skill, value)

    @staticmethod
    def messages(messages):
        return [{"role": "system" if message.type == "system" else "user", "content": message.content}
                for message in messages]

    def selector_messages(self, instruction, library):
        host = SimpleNamespace(benchmark_name="terminalbench")
        return self.messages(self.selector(host).build_prompt(instruction, [self.skill(item) for item in library]))

    def generation_messages(self, value):
        editor = self.editor(None, SimpleNamespace(version=0))
        return self.messages(editor.build_package_prompt(self.skill(value["base_skill"]), value["evidence"],
                                                       value.get("feedback", [])))

    def generation_outcome(self, value, raw):
        editor = self.editor(SimpleNamespace(llm=lambda *args, **kwargs: raw), SimpleNamespace(version=0))
        return self.schema.to_dict(editor.propose_package(self.skill(value["base_skill"]), value["evidence"],
                                                       value.get("feedback", [])))

    def stream_completion(self, path):
        data = Path(path).read_bytes()
        if not any(line.strip() == b"data: [DONE]" for line in data.splitlines()):
            raise ValueError("stream_missing_done")
        return self.relay.coalesce_sse([data])


class LibraryAudit:
    def __init__(self, root, native=None):
        self.root = Path(root).resolve()
        self.manifest = read(self.root / "manifest.json")
        self.remote = Path(self.manifest.get("remote_root", str(self.root)))
        self.native = native or NativeReplay(self.root)
        self.panel = read(self.root / "frozen/panel.json")
        self.seed = read(self.root / "frozen/seed_library.json")
        self.table = {row["task_name"]: row for row in read(self.root / "frozen/task_table.json")}
        self.template = read(self.root / "frozen/executor_template.json")
        self.selector_settings = read(self.root / "frozen/selector_settings.json")
        self.generation_settings = read(self.root / "frozen/generation_settings.json")
        self.excluded = set(read(self.root / "frozen/historical_sandbox_ids.json"))
        self.errors, self.pending, self.source_limits = [], [], []
        self.trials, self.generations, self.candidates = {}, [], []
        self.identities = defaultdict(list)
        self.allowed_slots, self.allowed_generations = set(), set()

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

    def issue(self, collection, path, code):
        path = Path(path)
        item = {"path": str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path),
                "code": code}
        if item not in collection:
            collection.append(item)

    def error(self, path, code):
        self.issue(self.errors, path, code)

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

    def same(self, path, actual, expected, code):
        if self.normalized(actual) != self.normalized(expected):
            self.error(path, code)
            return False
        return True

    def check_freeze(self):
        path = self.root / "bindings.json"
        if not path.exists():
            path = self.root / "frozen/bindings.json"
        bindings = self.optional(path, required=True) or []
        if not bindings:
            self.error(path, "empty_bindings")
        for item in bindings:
            current, snapshot = self.path(item["current"]), self.path(item["snapshot"])
            try:
                if current.resolve() == snapshot.resolve() or current.read_bytes() != snapshot.read_bytes():
                    self.error(current, "frozen_bytes_changed")
            except OSError:
                self.error(current, "frozen_binding_missing")
        expected = set(self.panel["development"] + self.panel["regression"])
        if set(self.table) != expected or len(expected) != 7 or len(self.panel["development"]) != 5:
            self.error(self.root / "frozen/panel.json", "task_split_drift")
        for task, row in self.table.items():
            split = "development" if task in self.panel["development"] else "regression"
            self.same(self.root / "frozen/task_table.json", row.get("split"), split, "task_split_mismatch")
            contract = self.root / "frozen/tasks" / task / "instruction.md"
            if contract.exists():
                self.same(contract, contract.read_text(), row.get("instruction"), "public_contract_differs_from_task")
        if len(self.seed) != 3 or len({item["skill_id"] for item in self.seed}) != 3:
            self.error(self.root / "frozen/seed_library.json", "library_topology_invalid")
        kwargs = self.template["agents"][0].get("kwargs", {})
        self.same(self.root / "frozen/executor_template.json", kwargs.get("model_info"),
                  {"max_input_tokens": 262144, "max_output_tokens": 65536}, "context_not_262144")
        if (self.root / "integrity_abort.json").exists():
            self.error(self.root / "integrity_abort.json", "integrity_abort")
        return len(bindings)

    def topology(self, library, path):
        fields = ("skill_id", "family_id", "name", "description")
        projection = lambda rows: sorted([{key: row[key] for key in fields} for row in rows],
                                         key=lambda item: item["skill_id"])
        try:
            self.same(path, projection(library), projection(self.seed), "library_routing_or_topology_changed")
            if len({item["skill_id"] for item in library}) != len(self.seed):
                self.error(path, "duplicate_library_skill")
        except (KeyError, TypeError):
            self.error(path, "malformed_library")

    def trial(self, slot, task, library, phase):
        initial_errors = len(self.errors)
        self.allowed_slots.add(slot)
        row = {"path": str(slot.relative_to(self.root)), "task": task, "phase": phase,
               "reserved": (slot / "reserved.json").exists(), "finished": False, "valid": False,
               "reward": None, "selected_skill_id": None, "tokens": measured(None)}
        self.trials[slot] = row
        if not row["reserved"]:
            self.issue(self.pending, slot, "required_slot_not_reserved")
            return row
        record = self.optional(slot / "record.json", required=True)
        if record is None:
            return row
        row.update(finished=True, error_type=record.get("error_type"), duration_seconds=record.get("duration_seconds"))
        if record.get("status") not in ("valid", "unresolved") or record.get("no_retry") is not True:
            self.error(slot / "record.json", "status_or_retry_invalid")
        if record.get("status") == "unresolved" and record.get("reward") is not None:
            self.error(slot / "record.json", "unresolved_reward_not_null")
        self.same(slot / "input.json", self.optional(slot / "input.json", required=True),
                  {"task": self.table[task], "phase": phase, "library": library}, "trial_input_drift")
        catalog = [{"skill_id": item["skill_id"], "description": item["description"]}
                   for item in sorted(library, key=lambda item: item["skill_id"])]
        self.same(slot / "catalog.json", self.optional(slot / "catalog.json", required=True), catalog, "catalog_drift")
        request = self.optional(slot / "selector_request.json", required=True)
        expected_request = {**self.selector_settings,
                            "messages": self.native.selector_messages(self.table[task]["instruction"], library)}
        self.same(slot / "selector_request.json", request, expected_request, "selector_request_not_native_or_settings_drift")
        response = self.optional(slot / "selector_response.json", required=True)
        selection = self.optional(slot / "selection.json", required=True)
        chosen = None
        if response is not None and selection is not None:
            raw = completion_text(response) if isinstance(response, (str, dict)) and not (
                isinstance(response, dict) and "error" in response and "raw" not in response) else ""
            stream_path = slot / "selector_response.sse"
            if isinstance(response, dict) and response.get("status") != "failed":
                try:
                    streamed = self.native.stream_completion(stream_path)
                    self.same(stream_path, completion_text(streamed), raw, "selector_completion_differs_from_stream")
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    self.error(stream_path, "successful_selector_stream_missing_or_incomplete")
            self.same(slot / "selection.json", selection.get("raw", ""), raw, "selector_raw_mismatch")
            chosen = self.native.parse_selection(raw, [item["skill_id"] for item in library])
            self.same(slot / "selection.json", selection.get("catalog"), catalog, "selection_catalog_drift")
            if chosen is None:
                if selection.get("ok") is not False or record.get("status") != "unresolved":
                    self.error(slot / "selection.json", "selection_failure_given_fallback_or_valid_reward")
            else:
                self.same(slot / "selection.json", selection.get("skill_id"), chosen, "selection_parse_mismatch")
                self.same(slot / "selection.json", selection.get("why", ""), self.native.parse_why(raw), "selection_why_mismatch")
                skill = next(item for item in library if item["skill_id"] == chosen)
                key = f"{chosen}@v{skill['version']}"
                if selection.get("ok") is not True or selection.get("load_stage") != "after_selection":
                    self.error(slot / "selection.json", "invalid_load_stage")
                self.same(slot / "selection.json", selection.get("loaded_skill_id"), chosen, "loaded_id_mismatch")
                self.same(slot / "selection.json", selection.get("loaded_skill_key"), key, "loaded_key_mismatch")
                self.same(slot / "loaded_skill.json", self.optional(slot / "loaded_skill.json", required=True), skill,
                          "loaded_package_differs_from_selected_library")
                self.same(slot / "record.json", record.get("selected_skill_id"), chosen, "record_selection_mismatch")
                self.same(slot / "record.json", record.get("loaded_skill_key"), key, "record_loaded_key_mismatch")
                row.update(selected_skill_id=chosen, loaded_skill_key=key)
        jobs = sorted((slot / "jobs").glob("*/*/result.json"))
        trial_dirs = [path for path in (slot / "jobs").glob("*/*") if path.is_dir() and "__" in path.name]
        row["actual_harbor_trials"] = len(trial_dirs)
        if len(trial_dirs) > 1 or len(list((slot / "jobs").glob("*/config.json"))) > 1:
            self.error(slot, "multiple_harbor_trials_or_retries")
        if chosen is not None:
            config = self.optional(slot / "harbor_config.json", required=True)
            if config:
                expected_config = self.native.runtime.build_config(self.template, self.root, slot, task, slot / "skills")
                self.same(slot / "harbor_config.json", config, expected_config, "full_harbor_configuration_drift")
                self.same(slot / "harbor_config.json", config.get("agents"), self.template["agents"], "harbor_agent_drift")
                if config.get("n_attempts") != 1 or config.get("orchestrator", {}).get("retry", {}).get("max_retries") != 0:
                    self.error(slot / "harbor_config.json", "harbor_retry_drift")
                if config.get("orchestrator", {}).get("n_concurrent_trials") != 1:
                    self.error(slot / "harbor_config.json", "harbor_concurrency_drift")
                datasets = config.get("datasets") or []
                if len(datasets) != 1 or datasets[0].get("task_names") != [task]:
                    self.error(slot / "harbor_config.json", "harbor_task_filter_drift")
                for path in (slot / "jobs").glob("*/config.json"):
                    self.same(path, self.optional(path, source=True), config, "actual_harbor_job_config_drift")
            self.optional(slot / "load_manifest.json", required=True)
        result_path = self.path(record["result_path"]) if record.get("result_path") else None
        if result_path and (not result_path.is_relative_to(slot) or result_path not in jobs):
            self.error(slot / "record.json", "result_outside_slot_or_not_official_job")
            result_path = None
        if result_path:
            snapshot = self.optional(result_path.parent / "snapshot.json", source=True)
            identity = snapshot.get("source_sandbox_id") if snapshot else None
            row["sandbox_id"] = identity
            if identity:
                self.identities[identity].append(slot)
                self.same(slot / "record.json", record.get("sandbox_id"), identity, "snapshot_identity_mismatch")
                if identity in self.excluded:
                    self.error(slot, "historical_sandbox_reused")
            elif record.get("status") == "valid":
                self.error(slot, "valid_without_authoritative_identity")
            result = self.optional(result_path, source=True)
            if result:
                expected_agent = dict(self.template["agents"][0])
                expected_agent["name"] = "terminus-2"
                self.same(result_path, result.get("config", {}).get("agent"), expected_agent, "official_agent_drift")
                self.same(result_path, result.get("config", {}).get("verifier"), self.template.get("verifier"), "official_verifier_drift")
                expected_environment = json.loads(json.dumps(self.template.get("environment", {})))
                expected_environment.setdefault("kwargs", {})["skill_root"] = str(slot / "skills")
                self.same(result_path, result.get("config", {}).get("environment"), expected_environment,
                          "official_environment_drift")
                if result.get("config", {}).get("task", {}).get("path"):
                    self.same(result_path, result["config"]["task"]["path"],
                              str(self.root / "frozen/tasks" / task), "official_task_path_drift")
            effective_path = result_path.parent / "agent/effective_agent_config.json"
            effective = self.optional(effective_path, source=True)
            effective_expected = {"max_input_tokens": 262144, "max_output_tokens": 65536,
                      "enable_summarize": True, "proactive_summarization_threshold": 8000,
                      "skills_dir": "/opt/openclaw-skills"}
            if effective:
                self.same(effective_path, {key: effective.get(key) for key in effective_expected},
                          effective_expected, "effective_context_or_summarization_drift")
            elif record.get("status") == "valid":
                self.error(effective_path, "valid_without_effective_config")
            trace_path = result_path.parent / "agent/trajectory.json"
            trace = self.optional(trace_path, source=True)
            if trace:
                row["tokens"] = measured(trace.get("final_metrics"))
                row["rounds"] = len(trace.get("steps", []))
                row["tool_calls"] = sum(len(step.get("tool_calls") or []) for step in trace.get("steps", []))
                if chosen:
                    resources = next(item for item in library if item["skill_id"] == chosen).get("resources", {})
                    row["resource_usage"] = [{"step_index": index, "resource": resource,
                                              "tool": call.get("function_name"), "arguments": call.get("arguments"),
                                              "observation_present": bool(step.get("observation")),
                                              "classification": "path_reference_not_proof_of_execution"}
                                             for index, step in enumerate(trace.get("steps", []))
                                             for call in step.get("tool_calls") or [] for resource in resources
                                             if resource in json.dumps(call.get("arguments", {}), ensure_ascii=False)]
            try:
                _, reward, trajectory = self.native.validator.validate_trial(result_path, task, activation=True,
                    model=self.template["agents"][0]["model_name"])
                if record.get("status") == "valid":
                    if isinstance(record.get("reward"), bool) or record.get("reward") != reward:
                        self.error(slot / "record.json", "official_reward_mismatch")
                    if not chosen:
                        self.error(slot / "record.json", "valid_without_native_choice")
                    self.same(slot / "record.json", record.get("trajectory_path"), trajectory, "trajectory_path_mismatch")
                    row.update(valid=True, reward=reward)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row["official_issue"] = str(exc)
                if record.get("status") == "valid":
                    self.error(slot / "record.json", "claimed_valid_failed_official_validation")
            if chosen and (result_path.parent / "agent/skill_activation.json").exists():
                try:
                    self.mount(slot, result_path, next(item for item in library if item["skill_id"] == chosen))
                except (OSError, ValueError, KeyError, TypeError):
                    self.error(slot, "unreadable_mount_evidence")
        elif record.get("status") == "valid":
            self.error(slot / "record.json", "claimed_valid_without_official_result")
        if len(self.errors) > initial_errors:
            row.update(valid=False, reward=None, audit_rejected=True)
        return row

    def mount(self, slot, result_path, skill):
        activation_path = result_path.parent / "agent/skill_activation.json"
        activation = read(activation_path)
        source = self.path(activation["source_path"])
        if not source.is_relative_to(slot) or source.name != "SKILL.md":
            self.error(activation_path, "mounted_source_outside_slot")
            return
        package = self.native.skill(skill)
        expected = {"SKILL.md": self.native.entrypoint(package, package=package.package_mode)}
        if package.package_mode:
            expected.update(package.resources)
        files = {path.relative_to(source.parent).as_posix(): path.read_bytes()
                 for path in source.parent.rglob("*") if path.is_file()}
        if any(path.is_symlink() for path in source.parent.rglob("*")):
            self.error(source.parent, "mounted_symlink")
        if files != {name: content.encode("utf-8") for name, content in expected.items()}:
            self.error(source.parent, "full_mounted_package_bytes_mismatch")
        manifest = self.optional(slot / "load_manifest.json", required=True)
        if manifest:
            self.same(slot / "load_manifest.json", manifest.get("files"), expected, "load_manifest_files_mismatch")
            self.same(slot / "load_manifest.json", manifest.get("source_path"), str(source), "load_manifest_source_mismatch")
            self.same(slot / "load_manifest.json", manifest.get("skill_path"), activation.get("skill_path"), "load_manifest_target_mismatch")
        self.same(activation_path, activation.get("load_stage"), "after_selection", "activation_load_stage_drift")

    def stage(self, folder, library, tasks, count, phase):
        return [self.trial(folder / "slots" / task / f"{index:02d}", task, library, phase)
                for task in tasks for index in range(1, count + 1)]

    def generation(self, folder, base, eligible):
        expected_library = {item["skill_id"]: item for item in base}
        for skill in base:
            skill_id = skill["skill_id"]
            directory = folder / "generation" / skill_id
            self.allowed_generations.add(directory)
            status = self.optional(directory / "generation_status.json", required=True)
            sources = sorted([row for row in eligible if row.get("selected_skill_id") == skill_id],
                             key=lambda row: (0 if row["phase"] == "baseline" else 1, row["path"]))
            reserved = (directory / "reserved.json").exists()
            self.generations.append({"path": str(directory.relative_to(self.root)), "skill_id": skill_id,
                                     "reserved": reserved, "status": status, "tokens": measured(None),
                                     "duration_seconds": (status or {}).get("seconds")})
            if status and status.get("no_retry") is not True:
                self.error(directory, "generation_no_retry_missing")
            if not sources:
                if reserved or (status or {}).get("status") != "skipped_no_selected_evidence":
                    self.error(directory, "generation_without_actual_selected_evidence")
                continue
            if not reserved:
                self.issue(self.pending, directory, "generation_not_reserved")
                continue
            value = self.optional(directory / "generation_input.json", required=True)
            if not value:
                continue
            source_paths = [row["path"] for row in sources]
            self.same(directory / "generation_input.json", value.get("source_slots"), source_paths, "generation_source_grouping_drift")
            self.same(directory / "generation_input.json", value.get("base_skill"), skill, "generation_base_drift")
            self.same(directory / "generation_input.json", value.get("feedback", []), [], "separate_feedback_not_frozen")
            evidence = value.get("evidence", {})
            actual_rows = evidence.get("historical_evidence", [])
            self.same(directory / "generation_input.json", [item.get("id") for item in actual_rows], source_paths, "evidence_slot_ids_drift")
            for item, source in zip(actual_rows, sources):
                slot = self.root / source["path"]
                if source["task"] not in self.panel["development"] or source["phase"] == "final":
                    self.error(directory, "regression_or_final_evidence")
                expected = {"task_name": source["task"], "selected_skill_id": skill_id,
                            "loaded_skill_key": source.get("loaded_skill_key"), "official_reward": source["reward"],
                            "loaded_skill": read(slot / "loaded_skill.json")}
                for key, expected_value in expected.items():
                    self.same(directory / "generation_input.json", item.get(key), expected_value, "evidence_" + key + "_drift")
                record = read(slot / "record.json")
                trace = self.optional(self.path(record["trajectory_path"]), source=True) if record.get("trajectory_path") else None
                observations = self.native.filter.project_tool_results(trace) if trace else []
                self.same(directory / "generation_input.json", item.get("tool_observations"), observations, "tool_projection_drift")
                if any(result.get("truncated") is not False
                       for observation in observations for result in observation.get("results", [])):
                    self.error(directory, "legal_generation_observation_truncated")
            contracts = [{"task_name": task, "public_contract": self.table[task]["instruction"]}
                         for task in self.panel["development"]]
            actual_contracts = evidence.get("public_contracts", [])
            if any(item not in contracts for item in actual_contracts):
                self.error(directory, "nondevelopment_public_contract")
            request = self.optional(directory / "request.json", required=True)
            self.same(directory / "request.json", request,
                      {**self.generation_settings, "messages": self.native.generation_messages(value)},
                      "generation_request_not_native_or_settings_drift")
            if not status:
                continue
            if status.get("status") == "failed":
                if status.get("no_retry") is not True:
                    self.error(directory, "failed_generation_retry_policy")
                continue
            completion = self.optional(directory / "completion.json", required=True)
            outcome = self.optional(directory / "outcome.json", required=True)
            if completion is not None and outcome is not None:
                self.generations[-1]["tokens"] = measured(completion.get("usage", {}) if isinstance(completion, dict) else None)
                try:
                    streamed = self.native.stream_completion(directory / "response.sse")
                    self.same(directory / "completion.json", completion, streamed, "generation_completion_differs_from_stream")
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    self.error(directory, "successful_generation_stream_missing_or_incomplete")
                replay = self.native.generation_outcome(value, completion_text(completion))
                self.same(directory / "outcome.json", without_creation_time(outcome), without_creation_time(replay),
                          "native_proposal_replay_mismatch")
                if outcome.get("candidate"):
                    expected_library[skill_id] = outcome["candidate"]["skill"]
        return [expected_library[item["skill_id"]] for item in base]

    def run(self):
        bindings = self.check_freeze()
        tasks = self.panel["development"] + self.panel["regression"]
        baseline = self.stage(self.root / "baseline", self.seed, tasks, 3, "baseline")
        eligible = [row for row in baseline if row["task"] in self.panel["development"] and row["selected_skill_id"]]
        base = self.seed
        for number in (1, 2):
            folder = self.root / "candidates" / f"{number:03d}"
            expected = self.generation(folder, base, eligible)
            library = self.optional(folder / "library.json", required=True)
            status = self.optional(folder / "candidate_status.json", required=True)
            if library is None or status is None:
                continue
            self.topology(library, folder / "library.json")
            self.same(folder / "library.json", library, expected, "candidate_not_native_composition")
            changed = [item["skill_id"] for item, old in zip(library, base)
                       if (item.get("body"), item.get("resources", {})) != (old.get("body"), old.get("resources", {}))]
            self.same(folder / "candidate_status.json", status.get("changed_skills"), changed, "changed_skills_mismatch")
            self.same(folder / "candidate_status.json", status.get("usable"), bool(changed), "candidate_usability_mismatch")
            rows = self.stage(folder / "dev", library, self.panel["development"], 2,
                              f"candidate_{number}_development") if changed else []
            self.candidates.append({"number": number, "usable": bool(changed), "library": library,
                                    "successes": sum(row["reward"] == 1 for row in rows),
                                    "valid": sum(row["valid"] for row in rows), "rewards": [row["reward"] for row in rows]})
            if changed:
                base = library
                eligible += [row for row in rows if row["selected_skill_id"]]
        usable = [item for item in self.candidates if item["usable"]]
        selected = min(usable, key=lambda item: (-item["successes"], item["number"]))["number"] if usable else None
        selection_path = self.root / "final/selection.json"
        if not selection_path.exists() and (self.root / "selection.json").exists():
            selection_path = self.root / "selection.json"
        selection = self.optional(selection_path, required=True)
        if selection:
            self.same(selection_path, selection.get("selected"), selected, "library_selection_mismatch")
            self.same(selection_path, selection.get("rule"), "max_official_development_successes_tie_earlier", "selection_rule_drift")
        final = []
        if selected is not None:
            library = next(item["library"] for item in self.candidates if item["number"] == selected)
            self.same(self.root / "final/library.json", self.optional(self.root / "final/library.json", required=True),
                      library, "final_library_not_selected_candidate")
            final = self.stage(self.root / "final", library, tasks, 3, "final")
        for identity, slots in self.identities.items():
            if len(slots) > 1:
                for slot in slots:
                    self.error(slot, "duplicate_authoritative_sandbox_id")
                    self.trials[slot].update(valid=False, reward=None, audit_rejected=True)
        for path in self.root.rglob("reserved.json"):
            if "frozen" in path.relative_to(self.root).parts or "snapshots" in path.relative_to(self.root).parts:
                continue
            if path.parent not in self.allowed_slots and path.parent not in self.allowed_generations:
                self.error(path, "unallocated_or_extra_reserved_slot")
        rows = list(self.trials.values())
        reserved = sum(row["reserved"] for row in rows)
        generation_reserved = sum(row["reserved"] for row in self.generations)
        if reserved > 62 or generation_reserved > 6:
            self.error(self.root, "planned_budget_exceeded")
        per_task = []
        for task in tasks:
            old = [row["reward"] for row in baseline if row["task"] == task]
            new = [row["reward"] for row in final if row["task"] == task]
            per_task.append({"task": task, "split": self.table[task]["split"], "old_rewards": old, "new_rewards": new,
                             "regression": regression_status(old, new) if task in self.panel["regression"] else None})
        frequencies = {phase: dict(Counter(row["selected_skill_id"] for row in rows
                                          if row["phase"] == phase and row["selected_skill_id"]))
                       for phase in sorted({row["phase"] for row in rows})}
        aggregates = {}
        for phase in ("baseline", "final"):
            phase_rows = [row for row in rows if row["phase"] == phase]
            for split in ("development", "regression", "all"):
                group = [row for row in phase_rows if split == "all" or self.table[row["task"]]["split"] == split]
                known = sum(row["reward"] is not None for row in group)
                successes = sum(row["reward"] == 1 for row in group)
                aggregates[phase + ":" + split] = {"slots": len(group), "valid": known, "successes": successes,
                    "unresolved": len(group) - known, "success_rate": successes / len(group)
                    if group and known == len(group) else None}
        return {"passed": not self.errors, "protocol_complete": not self.errors and not self.pending,
                "errors": self.errors, "pending": self.pending, "source_limits": self.source_limits,
                "frozen_binding_count": bindings, "reserved_executor_slots": reserved,
                "actual_harbor_trials": sum(row.get("actual_harbor_trials", 0) for row in rows),
                "generation_slots_reserved": generation_reserved, "selected_candidate": selected,
                "valid_trials": sum(row["valid"] for row in rows), "successes": sum(row["reward"] == 1 for row in rows),
                "unresolved_trials": sum(row["finished"] and row["reward"] is None for row in rows),
                "per_task": per_task, "selection_frequencies": frequencies,
                "aggregates": aggregates,
                "candidates": [{key: value for key, value in item.items() if key != "library"} for item in self.candidates],
                "trials": rows, "generations": self.generations, "measurements": measurements(rows),
                "generation_measurements": measurements([row for row in self.generations if row["reserved"]]),
                "interpretation": "Closed-set library pilot; one native selection and one loaded Skill per trial. "
                                  "Context 262144 is shared, not an evolution benefit. Resource references do not prove correctness."}


def report(result):
    rewards = lambda values: " ".join("?" if value is None else str(value) for value in values) or "not run"
    lines = ["# Library pilot results", "", result["interpretation"], "",
             f"Audit passed: {result['passed']}; protocol complete: {result['protocol_complete']}.",
             f"Reserved executor slots: {result['reserved_executor_slots']}/62 (authorized cap 66); "
             f"actual Harbor trials: {result['actual_harbor_trials']}; generation slots: {result['generation_slots_reserved']}/6.",
             f"Valid: {result['valid_trials']}; successes: {result['successes']}; unresolved: {result['unresolved_trials']}.",
             f"Selected complete-library candidate: {result['selected_candidate'] or 'no_usable_candidate'}.", "",
             "| Task | Split | Old | New | Regression |", "|---|---|---|---|---|"]
    for row in result["per_task"]:
        lines.append(f"| {row['task']} | {row['split']} | {rewards(row['old_rewards'])} | "
                     f"{rewards(row['new_rewards'])} | {row['regression'] or ''} |")
    lines += ["", "`?` remains unresolved; it is not reward zero. Final feedback never changes selection or packages.",
              "", "## Whole-library aggregates", "", "```json", json.dumps(result["aggregates"], indent=2), "```",
              "", "## Selection frequencies", "", "```json", json.dumps(result["selection_frequencies"], indent=2), "```",
              "", "## Measurements", "", "Concurrent trial duration sums are not elapsed wall time; monetary cost is unknown.",
              "", "```json", json.dumps(result["measurements"], indent=2), "```",
              "", "Generation measurements:", "", "```json", json.dumps(result["generation_measurements"], indent=2), "```",
              "", "## Audit issues", "", "```json", json.dumps({key: result[key] for key in ("errors", "pending", "source_limits")}, indent=2), "```", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = LibraryAudit(args.root).run()
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.report:
        args.report.write_text(report(result), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("passed", "protocol_complete", "reserved_executor_slots",
          "actual_harbor_trials", "valid_trials", "unresolved_trials", "selected_candidate", "errors", "pending")}))
    return 0 if result["protocol_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
