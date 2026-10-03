#!/usr/bin/env python3
"""Define and prepare the frozen model-role replacement experiment matrix.

This module only plans and prepares independent campaign directories.  The
existing ``run_campaign.py`` remains responsible for preflight, execution,
resume, and stage audits.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROLES = (
    "l1_executor",
    "cold_start",
    "l2_planner",
    "l2_editor",
    "l2_reviewer",
    "selector",
)
PRIMARY_ROLES = ("l1_executor", "cold_start", "l2_planner", "l2_reviewer")
OPTIONAL_ROLES = ("l2_editor", "selector")
SCHEMA = 1
CONCURRENCY = {
    "searchqa": {
        "cold_start_workers": 128,
        "family_discovery_workers": 128,
        "evolve_l1_workers": 128,
        "l2_review_workers": 8,
        "test_workers": 128,
    },
    "alfworld": {
        "cold_start_workers": 32,
        "family_discovery_workers": 32,
        "evolve_l1_workers": 32,
        "l2_review_workers": 8,
        "test_workers": 32,
    },
}
ROLE_FLAGS = {
    "l1_executor": "--l1-model",
    "cold_start": "--cold-start-model",
    "l2_planner": "--l2-planner-model",
    "l2_editor": "--l2-editor-model",
    "l2_reviewer": "--l2-reviewer-model",
    "selector": "--selector-model",
}


def model_map(default_model, strong_model, replaced_roles=()):
    """Return the complete role map for one condition."""
    if not isinstance(default_model, str) or not default_model.strip():
        raise ValueError("default_model must be a non-empty model name")
    if not isinstance(strong_model, str) or not strong_model.strip():
        raise ValueError("strong_model must be a non-empty model name")
    if default_model == strong_model:
        raise ValueError("default_model and strong_model must differ")
    roles = tuple(replaced_roles)
    unknown = sorted(set(roles) - set(ROLES))
    if unknown:
        raise ValueError("Unknown model roles: " + ", ".join(unknown))
    if len(set(roles)) != len(roles):
        raise ValueError("A condition cannot replace the same role twice")
    return {role: (strong_model if role in roles else default_model) for role in ROLES}


def condition_specs(include_optional=False):
    """Return the registered one-factor conditions in execution order."""
    roles = PRIMARY_ROLES + (OPTIONAL_ROLES if include_optional else ())
    return tuple([{"name": "baseline", "replaced_roles": []}] + [
        {"name": role + "-strong", "replaced_roles": [role]} for role in roles
    ])


def validate_conditions(conditions, required_roles=PRIMARY_ROLES):
    """Validate a matrix before any campaign directory is created."""
    if not isinstance(conditions, (list, tuple)) or not conditions:
        raise ValueError("At least one condition is required")
    names = [item.get("name") for item in conditions]
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("Every condition needs a non-empty name")
    if len(set(names)) != len(names):
        raise ValueError("Condition names must be unique")
    baseline = [item for item in conditions if item.get("name") == "baseline"]
    if len(baseline) != 1 or baseline[0].get("replaced_roles") != []:
        raise ValueError("Exactly one unreplaced baseline condition is required")
    seen = set()
    for item in conditions:
        replaced = item.get("replaced_roles")
        if not isinstance(replaced, list) or len(replaced) > 1:
            raise ValueError("This matrix only permits one-factor conditions")
        unknown = sorted(set(replaced) - set(ROLES))
        if unknown:
            raise ValueError("Unknown model roles: " + ", ".join(unknown))
        if replaced:
            seen.add(replaced[0])
    missing = sorted(set(required_roles) - seen)
    if missing:
        raise ValueError("Missing required single-role conditions: " + ", ".join(missing))
    return tuple(conditions)


def build_plan(default_model, strong_model, include_optional=False):
    """Build the immutable, human-readable experiment matrix."""
    conditions = condition_specs(include_optional)
    validate_conditions(conditions)
    return {
        "schema": SCHEMA,
        "kind": "model-role-replacement",
        "created": time.time(),
        "default_model": default_model,
        "strong_model": strong_model,
        "roles": list(ROLES),
        "primary_roles": list(PRIMARY_ROLES),
        "optional_roles": list(OPTIONAL_ROLES),
        "conditions": [
            {
                **item,
                "models": model_map(default_model, strong_model, item["replaced_roles"]),
            }
            for item in conditions
        ],
        "protocol": {
            "benchmarks": ["searchqa", "alfworld"],
            "splits": {"searchqa": [400, 200, 1400], "alfworld": [39, 18, 134]},
            "evolve_rounds": 2,
            "acceptance_mode": "predicted",
            "predicted_review_scope": "val",
            "skill_edit_mode": "rewrite",
            "batch_size": 50,
            "candidate_count": 3,
            "l2_review_workers": 8,
            "autonomous_attempts": 4,
            "supervised_attempts": 1,
            "request_interval_seconds": 0.5,
            "concurrency": CONCURRENCY,
            "test_is_held_out": True,
        },
    }


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def validate_plan(plan):
    if plan.get("schema") != SCHEMA or plan.get("kind") != "model-role-replacement":
        raise ValueError("Unsupported model-role matrix plan")
    model_map(plan["default_model"], plan["strong_model"], ())
    validate_conditions(plan["conditions"])
    for condition in plan["conditions"]:
        expected = model_map(plan["default_model"], plan["strong_model"],
                             condition["replaced_roles"])
        if condition.get("models") != expected:
            raise ValueError("Condition model map does not match its replacement roles")
    return plan


def prepare_commands(plan, root, inputs):
    """Return the exact side-effecting prepare command for every condition."""
    validate_plan(plan)
    root = Path(root).resolve()
    inputs = Path(inputs).resolve()
    runner = Path(__file__).with_name("run_campaign.py").resolve()
    commands = []
    for condition in plan["conditions"]:
        command = [
            sys.executable, str(runner), "prepare",
            "--root", str(root / condition["name"]), "--inputs", str(inputs),
            "--skill-edit-mode", plan["protocol"]["skill_edit_mode"],
            "--acceptance-mode", plan["protocol"]["acceptance_mode"],
            "--predicted-review-scope", plan["protocol"]["predicted_review_scope"],
        ]
        for role in ROLES:
            command.extend([ROLE_FLAGS[role], condition["models"][role]])
        commands.append(command)
    return commands


def test_commands(plan, root, python=None):
    """Return held-out test commands for every prepared condition/benchmark."""
    validate_plan(plan)
    root = Path(root).resolve()
    executable = python or os.environ.get("ALFWORLD_PYTHON") or sys.executable
    commands = []
    for condition in plan["conditions"]:
        condition_root = root / condition["name"]
        for benchmark in plan["protocol"]["benchmarks"]:
            commands.append([
                executable, str(condition_root / "code" / "run_campaign.py"),
                "test", "--root", str(condition_root),
                "--benchmark", benchmark,
            ])
    return commands


def prepare(plan, root, inputs, execute=False):
    """Write commands and optionally prepare each independent campaign root."""
    commands = prepare_commands(plan, root, inputs)
    root = Path(root).resolve()
    save(root / "matrix.json", plan)
    save(root / "prepare-commands.json", {"commands": commands})
    save(root / "test-commands.json", {"commands": test_commands(plan, root)})
    if not execute:
        return {"prepared": [], "commands": commands}
    prepared = []
    for condition, command in zip(plan["conditions"], commands):
        condition_root = root / condition["name"]
        if condition_root.exists() and any(condition_root.iterdir()):
            manifest_path = condition_root / "manifest.json"
            if not manifest_path.exists():
                raise ValueError("Existing condition directory has no manifest: " + str(condition_root))
            manifest = read(manifest_path)
            if (manifest.get("models") != condition["models"] or
                    not protocol_matches(plan, manifest)):
                raise ValueError("Existing condition directory does not match the matrix: " +
                                 str(condition_root))
            prepared.append(condition["name"])
            continue
        subprocess.run(command, check=True, cwd=str(Path(__file__).resolve().parents[1]))
        prepared.append(condition["name"])
    save(root / "prepared.json", {"status": "complete", "conditions": prepared,
                                   "finished": time.time()})
    return {"prepared": prepared, "commands": commands}


def status(plan, root):
    """Summarize preparation/execution state without selecting a winner."""
    validate_plan(plan)
    root = Path(root).resolve()
    rows = []
    shared_reference = None
    for condition in plan["conditions"]:
        condition_root = root / condition["name"]
        row = {"condition": condition["name"], "root": str(condition_root)}
        manifest_path = condition_root / "manifest.json"
        if not manifest_path.exists():
            row["status"] = "not_prepared"
            rows.append(row)
            continue
        manifest = read(manifest_path)
        expected = condition["models"]
        current = manifest.get("models") or {}
        fallback = manifest.get("model", "")
        actual = {role: current.get(role) or fallback for role in ROLES}
        row["models_match"] = actual == expected
        shared_files = {
            path: digest for path, digest in (manifest.get("files") or {}).items()
            if path.startswith("code/") or path.startswith("inputs/")
        }
        shared_identity = (
            manifest.get("source_commit"), manifest.get("llm_base_url"), shared_files
        )
        row["shared_inputs_code_match"] = (
            shared_reference is None or shared_identity == shared_reference
        )
        if shared_reference is None:
            shared_reference = shared_identity
        if not row["models_match"]:
            row["status"] = "model_mismatch"
        elif not row["shared_inputs_code_match"]:
            row["status"] = "shared_inputs_or_code_mismatch"
        elif not protocol_matches(plan, manifest):
            row["status"] = "protocol_mismatch"
        elif (condition_root / "full" / "complete.json").exists():
            row["status"] = ("test_complete" if test_complete(condition_root)
                              else "full_evolution_complete")
        elif (condition_root / "preflight" / "complete.json").exists():
            row["status"] = "preflight_complete"
        else:
            row["status"] = "prepared"
        rows.append(row)
    return {"conditions": rows}


def test_complete(condition_root):
    """Require an audited test summary for both benchmark runs."""
    summaries = []
    for benchmark in ("searchqa", "alfworld"):
        paths = list((condition_root / "full" / benchmark / "run" / "test").glob(
            "*/summary.json"))
        if len(paths) != 1:
            return False
        summary = read(paths[0])
        audit_path = paths[0].parent / "audit.json"
        if (summary.get("status") != "complete" or not audit_path.exists() or
                read(audit_path).get("integrity") != "passed"):
            return False
        summaries.append(summary)
    return len(summaries) == 2


def protocol_matches(plan, manifest):
    """Check the campaign fields that must be identical across conditions."""
    protocol = plan["protocol"]
    if (not manifest.get("llm_base_url") or
            manifest.get("evolve_rounds") != protocol["evolve_rounds"] or
            manifest.get("acceptance_mode", "predicted") != protocol["acceptance_mode"] or
            manifest.get("predicted_review_scope", "val") != protocol["predicted_review_scope"] or
            manifest.get("skill_edit_mode", "rewrite") != protocol["skill_edit_mode"] or
            manifest.get("batch_size") != protocol["batch_size"] or
            manifest.get("candidate_count") != protocol["candidate_count"] or
            manifest.get("autonomous_attempts", 4) != protocol["autonomous_attempts"] or
            manifest.get("supervised_attempts", 1) != protocol["supervised_attempts"] or
            manifest.get("request_interval_seconds") != protocol["request_interval_seconds"] or
            manifest.get("concurrency") != protocol["concurrency"]):
        return False
    concurrency = manifest.get("concurrency", {})
    for benchmark, expected in protocol["splits"].items():
        if manifest.get("benchmarks", {}).get(benchmark, {}).get("counts") != {
            "train": expected[0], "val": expected[1], "test": expected[2]
        }:
            return False
        settings = concurrency.get(benchmark)
        if not isinstance(settings, dict) or settings.get("l2_review_workers") != protocol["l2_review_workers"]:
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "prepare", "status"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--matrix", type=Path)
    parser.add_argument("--default-model", default=os.environ.get("EXPE_LLM_MODEL"))
    parser.add_argument("--strong-model", default="glm-5.3-ali")
    parser.add_argument("--include-optional", action="store_true")
    parser.add_argument("--execute", action="store_true",
                        help="Actually create each campaign directory during prepare")
    args = parser.parse_args(argv)

    if args.action == "plan":
        if not args.default_model:
            parser.error("--default-model or EXPE_LLM_MODEL is required")
        plan = build_plan(args.default_model, args.strong_model, args.include_optional)
        output = args.output or args.root / "matrix.json"
        save(output, plan)
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    matrix_path = args.matrix or args.root / "matrix.json"
    plan = validate_plan(read(matrix_path))
    if args.action == "prepare":
        if not args.inputs:
            parser.error("prepare requires --inputs")
        result = prepare(plan, args.root, args.inputs, execute=args.execute)
    else:
        result = status(plan, args.root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
