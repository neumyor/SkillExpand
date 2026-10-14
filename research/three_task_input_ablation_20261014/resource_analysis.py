"""Offline, conservative resource-use evidence from autonomous agent tool calls.

An invocation is a command the agent submitted, never proof it ran successfully.
Only agent trajectory JSON, package metadata and generation status are inputs.
No command in a trajectory is executed by this module.
"""
import argparse
import ast
import json
from pathlib import Path, PurePosixPath
import re
import shlex


RESTRICTED = re.compile(r"/tests|verifier|reward\.txt|test[-_]stdout", re.I)
ERROR = re.compile(r"Traceback \(most recent call last\)|(?:^|\n)(?:\w*Error:|bash:|sh:)|command not found|No such file or directory|Permission denied|SyntaxError|FAILED|Segmentation fault", re.I)
INTERPRETER = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|pypy\d*|bash|sh|zsh|dash|node|nodejs|ruby|perl|lua\d*|Rscript|php|julia|deno|bun)$")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def commands(arguments):
    """Extract only explicit command fields; do not reinterpret arbitrary prose."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return [arguments]
    if not isinstance(arguments, dict):
        return []
    if isinstance(arguments.get("commands"), list):
        output = []
        for item in arguments["commands"]:
            if isinstance(item, dict) and isinstance(item.get("keystrokes"), str):
                output.append(item["keystrokes"])
            elif isinstance(item, str):
                output.append(item)
        return output
    for key in ("command", "cmd", "keystrokes"):
        if isinstance(arguments.get(key), str):
            return [arguments[key]]
    return []


def _shell_segments(command):
    # Ignore heredoc payloads as shell commands; preserve the invocation line.
    lines = []
    delimiter = None
    for line in command.splitlines():
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        lines.append(line)
        match = re.search(r"<<-?\s*['\"]?([A-Za-z_][\w]*)['\"]?", line)
        if match:
            delimiter = match.group(1)
    lexer = shlex.shlex("\n".join(lines), posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segment = []
    try:
        for token in lexer:
            if token and all(c in ";&|\n" for c in token):
                if segment:
                    yield segment
                    segment = []
            else:
                segment.append(token)
        if segment:
            yield segment
    except ValueError:
        return


def _linked_results(step, call, call_count):
    results = (step.get("observation") or {}).get("results", [])
    call_id = call.get("tool_call_id") or call.get("id")
    exact = [r for r in results if call_id is not None and
             (r.get("tool_call_id") or r.get("call_id")) == call_id]
    if exact:
        return exact, "tool_call_id"
    if call_count == 1 and results:
        return results, "single_call_same_step"
    return [], "unlinked_same_step" if results else "no_result"


def _import_names(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def analyze_resource_use(trace, resources, mount_dir=None):
    """Classify submitted reads/copies/invocations with conservative outcomes."""
    resource_paths = list(resources)
    aliases = {}
    events = []

    def resolve(token):
        if token in aliases:
            return aliases[token][0], "copy_alias_unverified"
        for resource in resource_paths:
            if mount_dir and token == str(PurePosixPath(mount_dir) / resource):
                return resource, "mounted_absolute_path"
            if token == resource or token.endswith("/" + resource):
                return resource, "resource_relative_or_suffix_path"
        return None, None

    for step_index, step in enumerate(trace.get("steps", [])):
        calls = step.get("tool_calls") or []
        results = (step.get("observation") or {}).get("results", [])
        if any(RESTRICTED.search(s) for call in calls for s in _strings(call.get("arguments"))) or any(
            RESTRICTED.search(s) for result in results for s in _strings(result.get("content", ""))
        ):
            events.append({"step_index": step_index, "step_id": step.get("step_id"),
                           "classification": "restricted_evaluation_output_removed",
                           "outcome": "unknown"})
            continue
        for call_index, call in enumerate(calls):
            linked, linkage = _linked_results(step, call, len(calls))
            contents = "\n".join(s for result in linked for s in _strings(result.get("content", "")))
            error_signal = bool(ERROR.search(contents))
            exit_codes = [r.get("exit_code") for r in linked if isinstance(r.get("exit_code"), int)]
            for command_index, command in enumerate(commands(call.get("arguments"))):
                for segment_index, tokens in enumerate(_shell_segments(command)):
                    if not tokens:
                        continue
                    # Wrapper removal only for unambiguous executables, no eval.
                    actual = list(tokens)
                    while actual and (actual[0] in ("env", "command", "exec", "sudo") or re.match(r"^\w+=", actual[0])):
                        actual.pop(0)
                    if actual and actual[0] == "timeout" and len(actual) > 2 and re.match(r"^\d", actual[1]):
                        actual = actual[2:]
                    if not actual:
                        continue
                    executable = PurePosixPath(actual[0]).name
                    matches = [(i, *resolve(token)) for i, token in enumerate(actual) if resolve(token)[0]]
                    observed = {
                        "step_index": step_index, "step_id": step.get("step_id"),
                        "tool_call_index": call_index, "tool_call_id": call.get("tool_call_id") or call.get("id"),
                        "tool_function": call.get("function_name"), "command_index": command_index,
                        "segment_index": segment_index, "command": shlex.join(tokens),
                        "observed_result_present": bool(linked), "result_linkage": linkage,
                        "observed_error_signal": error_signal, "observed_exit_codes": exit_codes,
                        # A batch result/exit code is never enough to prove this specific resource's behavior.
                        "outcome": "unknown", "scope": "submitted_command_not_verified_execution",
                    }
                    for token_index, resource, match_kind in matches:
                        kind = "reference"
                        redirected_write = token_index > 0 and actual[token_index - 1] in (">", ">>", "2>", "&>")
                        if redirected_write:
                            kind = "write_reference"
                        elif executable in ("cat", "head", "tail", "less", "more", "sed", "awk", "grep", "rg") and token_index > 0:
                            kind = "read"
                        elif executable == "cp" and token_index > 0:
                            operands = [t for t in actual[1:] if not t.startswith("-")]
                            if len(operands) == 2 and actual[token_index] == operands[0]:
                                kind = "copy"
                                destination = operands[1]
                                if destination.endswith("/"):
                                    destination += PurePosixPath(operands[0]).name
                                aliases[destination] = (resource, len(events))
                        elif token_index == 0:
                            kind = "invocation"
                        elif INTERPRETER.fullmatch(executable) and token_index == 1:
                            kind = "invocation"
                        elif executable in ("go", "dotnet") and token_index == 2 and actual[1] in ("run", "script"):
                            kind = "invocation"
                        elif executable in ("source", ".") and token_index == 1:
                            kind = "invocation"
                        events.append({**observed, "resource": resource, "classification": kind,
                                       "resource_match": match_kind,
                                       "alias_establishment_unverified": kind == "copy"})
                    if executable.startswith(("python", "pypy")) and "-c" in actual:
                        position = actual.index("-c") + 1
                        if position < len(actual):
                            for name in _import_names(actual[position]):
                                for resource in resource_paths:
                                    if resource.endswith(".py") and name.replace(".", "/") == resource[:-3]:
                                        events.append({**observed, "resource": resource,
                                                       "classification": "import_invocation_candidate",
                                                       "resource_match": "module_name_resolution_unproven",
                                                       "import_name": name})
    return {"events": events, "classification_counts": {
        kind: sum(e["classification"] == kind for e in events)
        for kind in sorted({e["classification"] for e in events})
    }, "limits": ["Submitted invocation does not establish success or correct behavior.",
                    "Copy aliases are provisional; copy success, cwd and later overwrites may be unknown.",
                    "Same-step output is linked only for a single tool call or an exact call ID.",
                    "Imports are candidates; runtime module search-path resolution is not proven.",
                    "Complex shell expansion, interpreter flags and dynamically constructed commands may remain references or be missed."]}


def observed_failure_steps(trace):
    """Public agent-tool errors only; exclude restricted evaluation steps."""
    failures = []
    for index, step in enumerate(trace.get("steps", [])):
        calls = step.get("tool_calls") or []
        results = (step.get("observation") or {}).get("results", [])
        if any(RESTRICTED.search(s) for call in calls for s in _strings(call.get("arguments"))) or any(
            RESTRICTED.search(s) for result in results for s in _strings(result.get("content", ""))
        ):
            continue
        for result_index, result in enumerate(results):
            contents = "\n".join(_strings(result.get("content", "")))
            markers = [m.group(0).strip() for m in ERROR.finditer(contents)]
            if markers:
                failures.append({"step_index": index, "step_id": step.get("step_id"),
                                 "result_index": result_index,
                                 "tool_functions": [c.get("function_name") for c in calls],
                                 "error_markers": markers, "resource_causation": "unknown"})
    return failures


def read_json_artifact(path, recover_steps=False):
    """Return data plus explicit damage metadata; never invent missing records.

    For truncated agent trajectories only, recover complete step objects in the
    original top-level steps array. The partial final step is discarded.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as exc:
        return None, {"status": "unreadable", "error_type": type(exc).__name__}
    if not text.strip():
        return None, {"status": "empty", "bytes": path.stat().st_size}
    try:
        return json.loads(text), {"status": "complete"}
    except ValueError as exc:
        issue = {"status": "invalid_json", "error_type": type(exc).__name__,
                 "error_character": getattr(exc, "pos", None), "bytes": path.stat().st_size}
    if not recover_steps:
        return None, issue
    decoder = json.JSONDecoder()
    index = 0
    steps = []
    try:
        index = len(text) - len(text.lstrip())
        if text[index] != "{":
            return None, issue
        index += 1
        while True:
            while text[index].isspace() or text[index] == ",":
                index += 1
            key, index = decoder.raw_decode(text, index)
            while text[index].isspace():
                index += 1
            if text[index] != ":":
                return None, issue
            index += 1
            while text[index].isspace():
                index += 1
            if key != "steps":
                _, index = decoder.raw_decode(text, index)
                continue
            if text[index] != "[":
                return None, issue
            index += 1
            while True:
                while text[index].isspace() or text[index] == ",":
                    index += 1
                if text[index] == "]":
                    break
                value, index = decoder.raw_decode(text, index)
                if not isinstance(value, dict):
                    break
                steps.append(value)
            break
    except (ValueError, IndexError):
        pass
    if steps:
        return {"steps": steps}, {**issue, "status": "partial_steps_recovered",
                                 "complete_steps_recovered": len(steps),
                                 "partial_final_step_discarded": True}
    return None, issue


def analyze_experiment(root):
    """Collect available fixed-slot records, without opening evaluator output."""
    root = Path(root)
    rows = []
    generations = []
    for task_root in sorted((root / "tasks").iterdir()):
        if not task_root.is_dir():
            continue
        slots = sorted(p for p in (task_root / "baseline").glob("*") if p.is_dir())
        for arm in ("A", "B"):
            arm_root = task_root / "arms" / arm
            slots.extend(sorted(p for p in arm_root.glob("candidate-*/dev/*") if p.is_dir()))
            slots.extend(sorted(p for p in arm_root.glob("final/*") if p.is_dir()))
            for status_path in sorted(arm_root.glob("candidate-*/generation_status.json")):
                status, status_health = read_json_artifact(status_path)
                status = status or {}
                package, package_health = read_json_artifact(status_path.parent / "skill.json")
                generations.append({"slot": str(status_path.parent.relative_to(root)),
                                    "status": status.get("status", "unknown"),
                                    "error_type": status.get("error_type"),
                                    "no_retry": status.get("no_retry"),
                                    "status_artifact": status_health,
                                    "package_artifact": package_health,
                                    "resources": list((package or {}).get("resources", {}))})
        for directory in slots:
            record, record_health = read_json_artifact(directory / "record.json")
            record = record or {}
            trace_path = Path(record.get("trajectory_path") or "__missing__")
            permitted = trace_path.name == "trajectory.json" and trace_path.parent.name == "agent"
            # Synced records retain remote paths. Prefer the agent trace located
            # inside this exact slot; never resolve arbitrary external artifacts.
            local_traces = sorted(directory.glob("jobs/*/*/agent/trajectory.json"))
            if len(local_traces) == 1:
                trace_path = local_traces[0]
            elif permitted and trace_path.exists() and directory in trace_path.parents:
                pass
            else:
                trace_path = directory / "agent" / "trajectory.json"
            trace, trace_health = read_json_artifact(trace_path, recover_steps=True)
            trial_input, input_health = read_json_artifact(directory / "input.json")
            trial_input = trial_input or {}
            skill = trial_input.get("skill")
            skill_source = "trial_input"
            if skill is None:
                if "baseline" in directory.relative_to(task_root).parts:
                    package_path = task_root / "frozen" / "seed.json"
                elif "dev" in directory.relative_to(task_root).parts:
                    package_path = directory.parent.parent / "skill.json"
                else:
                    package_path = directory.parent / "skill.json"
                skill, _ = read_json_artifact(package_path)
                skill_source = "fixed_slot_package_fallback"
            skill = skill or {}
            resources = skill.get("resources", {})
            row = {"slot": str(directory.relative_to(root)), "status": record.get("status", "unknown"),
                   "reward": record.get("reward") if record.get("status") == "valid" else None,
                   "error_type": record.get("error_type"), "artifact_error_type": record.get("artifact_error_type"),
                   "trace_available": trace is not None, "resources": list(resources),
                   "record_artifact": record_health, "trace_artifact": trace_health,
                   "input_artifact": input_health, "skill_source": skill_source,
                   "trace_path": str(trace_path),
                   "failure_steps": observed_failure_steps(trace) if trace is not None else [],
                   "resource_analysis": analyze_resource_use(trace, resources) if trace is not None else None}
            rows.append(row)
    return {"model_calls": 0, "hidden_evaluation_data_read": False,
            "source_snapshot": str(root),
            "generation_statuses": generations, "trials": rows,
            "interpretation": "INTERIM available-artifact snapshot only. Missing, zero-byte or truncated records remain unknown; complete step objects recovered from truncated agent trajectories provide partial evidence only. Recorded rewards describe outcomes only. Tool error markers and resource invocations do not establish causes. Baseline/A/B contrasts require completed comparable slots; no-change and unresolved generation slots remain explicit."}


def render_markdown(report):
    lines = ["# Offline resource and failure-step evidence", "",
             report["interpretation"], "", "## Available evidence summary", ""]
    baselines = [r for r in report["trials"] if "/baseline/" in r["slot"]]
    lines.append(f"Baseline slots observed: {len(baselines)}; packaged resource lists empty: {sum(not r['resources'] for r in baselines)}. Empty resources do not imply successful task behavior.")
    for generation in report["generation_statuses"]:
        trials = [r for r in report["trials"] if r["slot"].startswith(generation["slot"] + "/dev/")]
        if not generation.get("resources"):
            continue
        events = [e for r in trials for e in (r.get("resource_analysis") or {}).get("events", [])]
        counts = {kind: sum(e["classification"] == kind for e in events) for kind in ("read", "copy", "invocation", "reference")}
        linked = sum(e.get("observed_result_present", False) for e in events if e["classification"] == "invocation")
        damaged = sum(r.get("trace_artifact", {}).get("status") != "complete" for r in trials)
        lines.append(f"- `{generation['slot']}` resources {', '.join(generation['resources'])}: submitted reads {counts['read']}, copies {counts['copy']}, invocations {counts['invocation']}, references {counts['reference']}; invocations with linked same-step results {linked}; incomplete traces {damaged}/{len(trials)}. These establish agent command evidence, not successful resource execution or a causal improvement.")
    lines.extend(["", "## Generation status", ""])
    for row in report["generation_statuses"]:
        lines.append(f"- `{row['slot']}`: {row['status']}" + (f" ({row['error_type']})" if row['error_type'] else "") + f"; resources {', '.join(row.get('resources', [])) or 'none recorded'}.")
    if not report["generation_statuses"]:
        lines.append("No generation status artifacts available.")
    for row in report["trials"]:
        lines.extend(["", f"## {row['slot']}", "",
                      f"Recorded status: {row['status']}; reward: {row['reward']}; agent trace available: {row['trace_available']}.", ""])
        lines.append(f"Record artifact: {row.get('record_artifact', {}).get('status')}; trace artifact: {row.get('trace_artifact', {}).get('status')}; recovered complete steps: {row.get('trace_artifact', {}).get('complete_steps_recovered', 'not needed or unavailable')}.")
        if row["resource_analysis"]:
            for event in row["resource_analysis"]["events"]:
                lines.append(f"- Step {event.get('step_id')} (index {event['step_index']}): {event['classification']}; resource `{event.get('resource', 'omitted')}`; observed result {event.get('observed_result_present', False)}; error signal {event.get('observed_error_signal', False)}; outcome unknown.")
        for failure in row["failure_steps"]:
            lines.append(f"- Public tool error at step {failure['step_id']} (index {failure['step_index']}), result {failure['result_index']}: {', '.join(failure['error_markers'])}; causal link unknown.")
        if not row["failure_steps"]:
            lines.append("No permitted public tool error marker identified; this does not establish correct behavior.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--trace", type=Path)
    mode.add_argument("--experiment", type=Path)
    parser.add_argument("--skill", type=Path)
    parser.add_argument("--mount-dir")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.experiment:
        report = analyze_experiment(args.experiment)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if args.markdown:
            args.markdown.write_text(render_markdown(report))
        return
    if args.skill is None:
        parser.error("--skill is required for --trace")
    if args.trace.name != "trajectory.json" or args.trace.parent.name != "agent":
        raise ValueError("Only explicit agent/trajectory.json inputs are supported")
    trace = json.loads(args.trace.read_text())
    skill = json.loads(args.skill.read_text())
    report = analyze_resource_use(trace, skill.get("resources", {}), args.mount_dir)
    report.update(trace=str(args.trace), skill=str(args.skill), model_calls=0,
                  hidden_evaluation_data_read=False)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
