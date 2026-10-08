#!/usr/bin/env python3
"""Prepare and launch an isolated E3/E4/E5 Terminal-Bench stage.

The script treats raw rollout files as immutable inputs.  It never converts an
infrastructure exception into reward=0; the coverage report records those rows
separately and the stage remains blocked until the required 89x3 input files
exist.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path


def coverage(source: Path, task_file: Path) -> dict:
    tasks = {row["task_name"] for row in json.loads(task_file.read_text())}
    rows = []
    counts: Counter[str] = Counter()
    for path in sorted(source.glob("**/result.json")):
        result = json.loads(path.read_text())
        task = result.get("task_name") or path.parent.name.split("__", 1)[0]
        if task not in tasks:
            continue
        exception = result.get("exception_info") or {}
        has_trajectory = (path.parent / "agent" / "trajectory.json").is_file()
        # Harbor job summaries sit one directory above the actual trial and
        # have child trajectories; count only the leaf result once.
        if not has_trajectory and any(path.parent.glob("*/agent/trajectory.json")):
            continue
        counts[task] += 1
        rows.append({
            "task_name": task,
            "attempt_index": counts[task],
            "result_path": str(path),
            "trajectory_path": str(path.parent / "agent" / "trajectory.json"),
            "raw_present": has_trajectory,
            "infrastructure_error": bool(exception),
            "exception_type": exception.get("exception_type"),
            "source_model": (result.get("config", {}).get("agent", {})
                              .get("model_name")),
        })
    complete = len(rows) == 267 and set(counts) == tasks and all(v == 3 for v in counts.values())
    # Raw input validity is path/coverage based. Infrastructure exceptions are
    # retained as metadata and never converted into reward=0.
    valid = sum(row["raw_present"] for row in rows)
    valid_complete = complete and valid == 267
    return {
        "tasks": len(counts), "expected_tasks": 89, "raw_rollout_count": len(rows),
        "valid_rollout_count": valid, "expected_rollout_count": 267,
        "complete_raw_coverage": complete,
        "complete_valid_coverage": valid_complete,
        "coverage": "89 tasks x 3 raw attempts (exceptions annotated)" if valid_complete else ("89 tasks x 3 raw attempts" if complete else "incomplete"),
        "infrastructure_rows": sum(row["infrastructure_error"] for row in rows),
        "incomplete_tasks": sorted(task for task in tasks if counts[task] != 3),
        "rows": rows,
    }


def write_status(root: Path, stage: str, state: str, report: dict, **extra: object) -> None:
    value = {"stage": stage, "status": state, "input_coverage": report, **extra}
    (root / "status.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_audit(root: Path, stage: str, report: dict, status: str, reason: str | None = None) -> None:
    value = {
        "stage": stage,
        "status": status,
        "gate": "89 tasks x 3 raw rollouts with exception annotations",
        "raw_rollout_count": report.get("raw_rollout_count"),
        "valid_rollout_count": report.get("valid_rollout_count"),
        "complete_raw_coverage": report.get("complete_raw_coverage"),
        "complete_valid_coverage": report.get("complete_valid_coverage"),
        "infrastructure_rows": report.get("infrastructure_rows"),
        "provenance_fields": ["source_model", "trajectory_path", "task_name", "attempt_index"],
    }
    if reason:
        value["reason"] = reason
    (root / "audit.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare(args: argparse.Namespace) -> int:
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    report = coverage(args.source_root.resolve(), args.task_file.resolve())
    (root / "input_coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["complete_valid_coverage"]:
        reason = "raw rollout coverage is not 89x3; result/trajectory inputs are missing"
        write_status(root, args.stage, "blocked_before_rollout", report, reason=reason)
        write_audit(root, args.stage, report, "blocked", reason)
        print(json.dumps({"status": "blocked_before_rollout", **{k: report[k] for k in ("raw_rollout_count", "valid_rollout_count", "infrastructure_rows")}}, indent=2))
        return 2
    # Existing prepared directories have frozen cold-start artifacts. Reuse
    # them; importing again would intentionally fail rather than mutate inputs.
    if not (root / "manifest.json").is_file():
        cmd = [args.python, str(Path(__file__).with_name("import_terminalbench_batch.py")),
               "--source-root", str(args.source_root), "--task-file", str(args.task_file),
               "--run-dir", str(root), "--source-model", args.source_model,
               "--method-model", args.method_model]
        subprocess.run(cmd, check=True)
    if args.skill_source and not (root / "skill_source.json").is_file():
        source = args.skill_source.resolve()
        for name in ("initial_skills.json", "skills.jsonl", "cold_start_complete.json", "library_manifest.json"):
            src = source / name
            if src.is_file():
                shutil.copyfile(src, root / name)
        (root / "skill_source.json").write_text(json.dumps({"source_run": str(source)}, indent=2) + "\n")
    write_status(root, args.stage, "prepared", report, source_model=args.source_model, method_model=args.method_model)
    write_audit(root, args.stage, report, "prepared")
    print(json.dumps({"status": "prepared", "stage": args.stage, "run_dir": str(root)}, indent=2))
    return 0


def launch(args: argparse.Namespace) -> int:
    root = args.run_dir.resolve()
    report = json.loads((root / "input_coverage.json").read_text())
    if not report.get("complete_valid_coverage"):
        reason = "raw rollout gate not met"
        write_status(root, args.stage, "blocked_before_rollout", report, reason=reason)
        write_audit(root, args.stage, report, "blocked", reason)
        return 2
    env = os.environ.copy()
    env["TBENCH_PERSIST_SANDBOXES"] = "0"
    worker_count = max(1, int(env.get("TB21_WORKERS", "100")))
    cmd = [args.python, "-m", "skillexpand", "--benchmark", "terminalbench",
           "--run-dir", str(root), "--phase", "evolve", "--resume",
           "--progressive-library", "--acceptance-panel", "all_train",
           "--acceptance-mode", "predicted", "--predicted-review-scope", "val",
           "--evolve-rounds", "1", "--candidate-count", "3", "--batch-size", "50",
           "--autonomous-attempts", "3", "--supervised-attempts", "0",
           "--evolve-l1-workers", str(worker_count), "--l2-review-workers", str(worker_count), "--test-workers", str(worker_count),
           "--l1-model", args.executor_model, "--cold-start-model", args.method_model,
           "--l2-planner-model", args.method_model, "--l2-editor-model", args.method_model,
           "--l2-reviewer-model", args.method_model, "--selector-model", args.method_model,
           "--llm-relay"]
    log = (root / "stage.log").open("ab")
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    (root / "stage.pid").write_text(f"{proc.pid}\n")
    write_status(root, args.stage, "running", report, pid=proc.pid,
                 executor_model=args.executor_model, method_model=args.method_model)
    write_audit(root, args.stage, report, "running")
    print(json.dumps({"status": "running", "stage": args.stage, "pid": proc.pid}, indent=2))
    return 0


def prepare_mixed(args: argparse.Namespace) -> int:
    """Prepare E5's immutable two-source cold-start ledger without launching it."""
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    left = coverage(args.source_root.resolve(), args.task_file.resolve())
    right = coverage(args.source_root2.resolve(), args.task_file.resolve())
    report = {
        "stage": "E5",
        "expected_sources": [args.source_model, args.source_model2],
        "sources": {args.source_model: left, args.source_model2: right},
        "complete_valid_coverage": left["complete_valid_coverage"] and right["complete_valid_coverage"],
    }
    (root / "input_coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["complete_valid_coverage"]:
        reason = "both source libraries must have 267 raw rollouts"
        write_status(root, "E5", "blocked_before_rollout", report, reason=reason)
        (root / "audit.json").write_text(json.dumps({"stage": "E5", "status": "blocked", "gate": "both sources: 89 tasks x 3 raw rollouts", "sources": {k: {"valid_rollout_count": v["valid_rollout_count"], "complete_valid_coverage": v["complete_valid_coverage"], "infrastructure_rows": v["infrastructure_rows"]} for k, v in report["sources"].items()}, "reason": reason}, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": "blocked_before_rollout", "sources": {
            args.source_model: left["valid_rollout_count"], args.source_model2: right["valid_rollout_count"]}}, indent=2))
        return 2
    rows = []
    for source_name, source_report in ((args.source_model, left), (args.source_model2, right)):
        for row in source_report["rows"]:
            if row["raw_present"]:
                rows.append({**row, "source_model": source_name})
    keys = [(r["task_name"], r["attempt_index"], r["source_model"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate E5 source/task/attempt key")
    (root / "mixed_trial_manifest.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in sorted(rows, key=lambda r: (r["source_model"], r["task_name"], r["attempt_index"]))))
    write_status(root, "E5", "prepared", report, source_models=[args.source_model, args.source_model2])
    (root / "audit.json").write_text(json.dumps({"stage": "E5", "status": "prepared", "gate": "both sources: 89 tasks x 3 raw rollouts", "records": len(rows), "provenance_fields": ["source_model", "trajectory_path", "task_name", "attempt_index"]}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "prepared", "stage": "E5", "records": len(rows), "run_dir": str(root)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "prepare-mixed", "launch"))
    parser.add_argument("--stage", choices=("E3", "E4", "E5"), required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-root2", type=Path)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-model", required=True)
    parser.add_argument("--source-model2")
    parser.add_argument("--method-model", required=True)
    parser.add_argument("--executor-model", default="qwen3.6-flash-distill")
    parser.add_argument("--skill-source", type=Path)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    if args.action == "prepare-mixed":
        if not args.source_root2 or not args.source_model2:
            parser.error("prepare-mixed requires --source-root2 and --source-model2")
        return prepare_mixed(args)
    return prepare(args) if args.action == "prepare" else launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
