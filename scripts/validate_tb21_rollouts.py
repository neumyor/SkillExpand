#!/usr/bin/env python3
"""Validate the immutable raw TerminalBench rollout input (default 89 tasks x 3)."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

INFRA_MARKERS = ("timeout", "connection", "disconnect", "sandbox", "runtime", "429",
                 "ratelimit", "persistence")


def _is_valid(result: dict, trial_dir: Path) -> tuple[bool, str]:
    if not (trial_dir / "agent" / "trajectory.json").is_file():
        return False, "missing_trajectory"
    exception = result.get("exception_info") or {}
    if exception:
        text = (f"{str(exception.get('exception_type', '')).lower()} "
                f"{str(exception.get('exception_message', '')).lower()}")
        if any(marker in text for marker in INFRA_MARKERS):
            return True, "infrastructure"
        return True, "runtime"
    verifier = result.get("verifier_result") or {}
    if "rewards" not in verifier or "reward" not in verifier.get("rewards", {}):
        return True, "missing_reward"
    return True, "valid"


def validate(source: Path, task_names: set[str] | None = None, attempts: int = 3) -> dict:
    rows = []
    by_task: Counter[str] = Counter()
    invalid: Counter[str] = Counter()
    for result_path in sorted(source.glob("*/result.json")):
        trial = result_path.parent
        result = json.loads(result_path.read_text())
        task = result.get("task_name") or trial.name.split("__", 1)[0]
        if task_names is not None and task not in task_names:
            continue
        ok, reason = _is_valid(result, trial)
        by_task[task] += 1
        if not ok:
            invalid[reason] += 1
        rows.append({"task_name": task, "trial_dir": str(trial), "valid": ok, "reason": reason})
    missing_tasks = sorted(task_names - set(by_task)) if task_names is not None else []
    incomplete = sorted(task for task, count in by_task.items() if count != attempts)
    valid_count = sum(row["valid"] for row in rows)
    expected = len(task_names) * attempts if task_names is not None else None
    complete = valid_count == expected and not missing_tasks and not incomplete
    return {
        "source_root": str(source),
        "tasks": len(by_task),
        "expected_tasks": len(task_names) if task_names is not None else None,
        "trials": len(rows),
        "valid_rollout_count": valid_count,
        "expected_valid_rollout_count": expected,
        "coverage": (f"{len(task_names)} tasks x {attempts} attempts" if complete else "incomplete"),
        "missing_tasks": missing_tasks,
        "incomplete_tasks": incomplete,
        "exception_or_metadata_by_reason": dict(invalid),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    tasks = {row["task_name"] for row in json.loads(args.task_file.read_text())}
    report = validate(args.source_root.resolve(), tasks, args.attempts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("valid_rollout_count", "expected_valid_rollout_count",
                                             "coverage", "exception_or_metadata_by_reason")}, indent=2))
    ok = (report["valid_rollout_count"] == report["expected_valid_rollout_count"]
          and not report["missing_tasks"] and not report["incomplete_tasks"])
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
