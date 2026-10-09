#!/usr/bin/env python3
"""Prepare and launch an isolated TB-eval stage (E3/E4) on TerminalBench.

Raw rollout files are immutable inputs.  This script never converts an
infrastructure exception into reward=0: the coverage report records those rows
separately, and the stage stays blocked until the required task x attempt input
files exist.  E5 (mixed two-source) is intentionally not supported.
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

STAGES = ("E3", "E4")
SKILL_SOURCE_FILES = ("initial_skills.json", "skills.jsonl", "cold_start_complete.json",
                      "library_manifest.json")


def coverage(source: Path, task_file: Path, expected_tasks: int = 89, attempts: int = 3) -> dict:
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
            "source_model": (result.get("config", {}).get("agent", {}).get("model_name")),
        })
    expected_rollouts = expected_tasks * attempts
    complete = (len(rows) == expected_rollouts and set(counts) == tasks
                and len(tasks) == expected_tasks and all(v == attempts for v in counts.values()))
    # Raw input validity is path/coverage based. Infrastructure exceptions are
    # retained as metadata and never converted into reward=0.
    valid = sum(row["raw_present"] for row in rows)
    valid_complete = complete and valid == expected_rollouts
    label = f"{expected_tasks} tasks x {attempts} raw attempts"
    return {
        "tasks": len(counts), "expected_tasks": expected_tasks, "raw_rollout_count": len(rows),
        "valid_rollout_count": valid, "expected_rollout_count": expected_rollouts,
        "attempts": attempts,
        "complete_raw_coverage": complete,
        "complete_valid_coverage": valid_complete,
        "coverage": f"{label} (exceptions annotated)" if valid_complete else (label if complete else "incomplete"),
        "infrastructure_rows": sum(row["infrastructure_error"] for row in rows),
        "incomplete_tasks": sorted(task for task in tasks if counts[task] != attempts),
        "rows": rows,
    }


def write_status(root: Path, stage: str, state: str, report: dict, **extra: object) -> None:
    value = {"stage": stage, "status": state, "input_coverage": report, **extra}
    (root / "status.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_audit(root: Path, stage: str, report: dict, status: str, reason: str | None = None) -> None:
    value = {
        "stage": stage,
        "status": status,
        "gate": f"{report.get('expected_tasks')} tasks x {report.get('attempts')} raw rollouts "
                "with exception annotations",
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
    report = coverage(args.source_root.resolve(), args.task_file.resolve(),
                      args.expected_tasks, args.attempts)
    (root / "input_coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["complete_valid_coverage"]:
        reason = (f"raw rollout coverage is not {args.expected_tasks}x{args.attempts}; "
                  "result/trajectory inputs are missing")
        write_status(root, args.stage, "blocked_before_rollout", report, reason=reason)
        write_audit(root, args.stage, report, "blocked", reason)
        print(json.dumps({"status": "blocked_before_rollout",
                          **{k: report[k] for k in ("raw_rollout_count", "valid_rollout_count",
                                                    "infrastructure_rows")}}, indent=2))
        return 2
    # Existing prepared directories have frozen cold-start artifacts. Reuse
    # them; importing again would intentionally fail rather than mutate inputs.
    if not (root / "manifest.json").is_file():
        cmd = [args.python, str(Path(__file__).with_name("import_terminalbench_batch.py")),
               "--source-root", str(args.source_root), "--task-file", str(args.task_file),
               "--run-dir", str(root), "--source-model", args.source_model,
               "--method-model", args.method_model,
               "--expected-tasks", str(args.expected_tasks), "--attempts", str(args.attempts)]
        subprocess.run(cmd, check=True)
    if args.skill_source and not (root / "skill_source.json").is_file():
        source = args.skill_source.resolve()
        for name in SKILL_SOURCE_FILES:
            src = source / name
            if src.is_file():
                shutil.copyfile(src, root / name)
        (root / "skill_source.json").write_text(json.dumps({"source_run": str(source)}, indent=2) + "\n")
    write_status(root, args.stage, "prepared", report,
                 source_model=args.source_model, method_model=args.method_model)
    write_audit(root, args.stage, report, "prepared")
    print(json.dumps({"status": "prepared", "stage": args.stage, "run_dir": str(root)}, indent=2))
    return 0


def build_launch_command(python: str, run_dir: Path, executor_model: str, method_model: str,
                         workers: int) -> list[str]:
    """The exact evolve command line; every protocol flag is fixed here, not by the caller."""
    w = str(max(1, int(workers)))
    return [python, "-m", "skillexpand", "--benchmark", "terminalbench",
            "--run-dir", str(run_dir), "--phase", "evolve", "--resume",
            "--progressive-library",
            "--acceptance-mode", "predicted", "--skill-edit-mode", "rewrite",
            "--evolve-rounds", "1", "--candidate-count", "3", "--batch-size", "50",
            "--autonomous-attempts", "3", "--supervised-attempts", "0",
            "--evolve-l1-workers", w, "--l2-review-workers", w, "--test-workers", w,
            "--l1-model", executor_model, "--cold-start-model", method_model,
            "--l2-planner-model", method_model, "--l2-editor-model", method_model,
            "--l2-reviewer-model", method_model, "--selector-model", method_model,
            "--llm-relay"]


def launch_env(base: dict) -> dict:
    env = dict(base)
    env["TBENCH_PERSIST_SANDBOXES"] = "0"
    return env


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def missing_library(root: Path) -> str | None:
    """Why the run has no model-generated M0 library, or None when it has one.

    The importer only writes a hand-written bootstrap Skill; evolving that is
    not the experiment, so M0 (propose + materialize) must have replaced it.
    """
    path = root / "library_manifest.json"
    if not path.is_file():
        return "M0 library is missing: run propose/materialize before launch"
    listed = [row["skill_id"] for row in json.loads(path.read_text())["skills"]]
    initial = [row["skill_id"] for row in json.loads((root / "initial_skills.json").read_text())]
    if listed != initial:
        return "initial_skills.json is not the materialized M0 library"
    return None


def launch(args: argparse.Namespace) -> int:
    root = args.run_dir.resolve()
    report = json.loads((root / "input_coverage.json").read_text())
    reason = (missing_library(root) if report.get("complete_valid_coverage")
              else "raw rollout gate not met")
    if reason:
        write_status(root, args.stage, "blocked_before_rollout", report, reason=reason)
        write_audit(root, args.stage, report, "blocked", reason)
        print(json.dumps({"status": "blocked_before_rollout", "reason": reason}))
        return 2
    pidfile = root / "stage.pid"
    # Liveness comes from the recorded child PID, never from pgrep on a script name.
    if pidfile.is_file() and _alive(int(pidfile.read_text().strip() or 0)):
        print(json.dumps({"status": "already_running", "pid": int(pidfile.read_text())}))
        return 3
    env = launch_env(os.environ)
    cmd = build_launch_command(args.python, root, args.executor_model, args.method_model,
                               int(env.get("TB21_WORKERS", "100")))
    with (root / "stage.log").open("ab") as log:
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    pidfile.write_text(f"{proc.pid}\n")
    write_status(root, args.stage, "running", report, pid=proc.pid,
                 executor_model=args.executor_model, method_model=args.method_model)
    write_audit(root, args.stage, report, "running")
    print(json.dumps({"status": "running", "stage": args.stage, "pid": proc.pid}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "launch"))
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--source-root", type=Path, help="required for prepare")
    parser.add_argument("--task-file", type=Path, help="required for prepare")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-model", help="required for prepare")
    parser.add_argument("--method-model", required=True)
    parser.add_argument("--executor-model", default="qwen3.6-flash-distill")
    parser.add_argument("--skill-source", type=Path)
    parser.add_argument("--expected-tasks", type=int, default=89)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    if args.action == "prepare":
        for name in ("source_root", "task_file", "source_model"):
            if getattr(args, name) is None:
                parser.error(f"prepare requires --{name.replace('_', '-')}")
        return prepare(args)
    return launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
