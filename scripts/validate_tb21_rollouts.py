#!/usr/bin/env python3
"""Validate the immutable 89 x 3 raw Terminal-Bench rollout input."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def _is_valid(result: dict, trial_dir: Path) -> tuple[bool, str]:
    if not (trial_dir / "agent" / "trajectory.json").is_file():
        return False, "missing_trajectory"
    try:
        if not json.loads((trial_dir / "agent" / "trajectory.json").read_text()):
            return False, "empty_trajectory"
    except (ValueError, OSError):
        return False, "invalid_trajectory"
    exception = result.get("exception_info") or {}
    if exception:
        exception_type = str(exception.get("exception_type", "")).lower()
        exception_message = str(exception.get("exception_message", "")).lower()
        text = f"{exception_type} {exception_message}"
        if any(marker in text for marker in ("timeout", "timed out")):
            return True, "timeout_accepted"
        for markers, reason in (
            (("429", "ratelimit", "rate limit"), "rate_limit"),
            (("503", "502", "serviceunavailable", "overloaded"), "provider_http"),
            (("connection", "disconnect"), "connection"),
            (("persistence",), "persistence"),
            (("verifier",), "verifier"),
            (("sandbox", "container", "environmentsetup"), "sandbox"),
        ):
            if any(marker in text for marker in markers):
                return False, reason
        return False, "runtime"
    verifier = result.get("verifier_result") or {}
    if "rewards" not in verifier or "reward" not in verifier.get("rewards", {}):
        return False, "missing_reward"
    reward = verifier["rewards"]["reward"]
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or reward not in (0, 1):
        return False, "invalid_reward"
    return True, "valid"


def validate(source: Path, task_names: set[str] | None = None,
             task_ids: dict[str, int] | None = None) -> dict:
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
        rows.append({"task_name": task, "task_id": (task_ids or {}).get(task),
                     "attempt_index": by_task[task], "trial_dir": str(trial),
                     "result_path": str(result_path),
                     "trajectory_path": str(trial / "agent/trajectory.json"),
                     "valid": ok, "reason": reason})
    missing_tasks = sorted(task_names - set(by_task)) if task_names is not None else []
    incomplete = sorted(task for task, count in by_task.items() if count != 3)
    valid_count = sum(row["valid"] for row in rows)
    report = {
        "source_root": str(source),
        "tasks": len(by_task),
        "expected_tasks": len(task_names) if task_names is not None else None,
        "trials": len(rows),
        "valid_rollout_count": valid_count,
        "expected_valid_rollout_count": 267,
        "coverage": "89 tasks x 3 attempts" if valid_count == 267 and not missing_tasks and not incomplete else "incomplete",
        "missing_tasks": missing_tasks,
        "incomplete_tasks": incomplete,
        "exception_or_metadata_by_reason": dict(invalid),
        "rows": rows,
        "attempt_index_basis": "one-based sorted trial-directory order per task, matching Harbor importer",
        "timeout_policy": "user-authorized: timeout outcomes count as valid raw rollouts; rewards unchanged",
        "accepted_timeout_count": sum(row['reason'] == 'timeout_accepted' for row in rows),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    task_ids = {row["task_name"]: index for index, row in
                enumerate(json.loads(args.task_file.read_text()))}
    report = validate(args.source_root.resolve(), set(task_ids), task_ids)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("valid_rollout_count", "expected_valid_rollout_count", "coverage", "exception_or_metadata_by_reason")}, indent=2))
    return 0 if report["valid_rollout_count"] == 267 and not report["missing_tasks"] and not report["incomplete_tasks"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
