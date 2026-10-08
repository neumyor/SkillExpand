#!/usr/bin/env python3
"""Rebuild paired held-out results for every completed model-role condition."""

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_role_matrix import read, save, validate_plan, status as matrix_status


def expected_test_ids(run):
    split = read(Path(run) / "split.json")
    return {
        int(task_id)
        for task_id, part in split["assignment"].items()
        if part == "test"
    }


def test_artifact(condition_root, benchmark):
    run = Path(condition_root) / "full" / benchmark / "run"
    targets = list((run / "test").glob("*/summary.json"))
    if len(targets) != 1:
        raise ValueError(f"{benchmark}: expected exactly one test summary")
    target = targets[0].parent
    summary = read(targets[0])
    audit = read(target / "audit.json")
    if summary.get("status") != "complete" or audit.get("integrity") != "passed":
        raise ValueError(f"{benchmark}: test summary/audit is incomplete")
    expected = expected_test_ids(run)
    scores_path = target / "scores.jsonl"
    rows = [json.loads(line) for line in scores_path.read_text().splitlines()]
    scores = {}
    for row in rows:
        task_id = int(row["task_id"])
        if task_id in scores:
            raise ValueError(f"{benchmark}: duplicate score for task {task_id}")
        if type(row.get("success")) is not bool or row.get("failure"):
            raise ValueError(f"{benchmark}: invalid score for task {task_id}")
        scores[task_id] = row["success"]
    failures = {int(task_id) for task_id in summary.get("routing_failures", [])}
    if scores.keys() & failures:
        raise ValueError(f"{benchmark}: routing failure also has an execution score")
    outcomes = dict(scores)
    outcomes.update({task_id: False for task_id in failures})
    if set(outcomes) != expected:
        raise ValueError(f"{benchmark}: test task coverage differs from frozen split")
    if summary.get("tasks") != len(expected):
        raise ValueError(f"{benchmark}: summary task count differs from frozen split")

    route_dir = run / "routes" / "test"
    route_tasks = {}
    for path in sorted((route_dir / "tasks").glob("*.json")):
        record = read(path)
        task_id = int(record["task_id"])
        choice = record.get("selection") or {}
        route_tasks[task_id] = choice.get("skill_id") if choice.get("ok") else None
    if set(route_tasks) != expected:
        raise ValueError(f"{benchmark}: frozen route coverage differs from test split")
    return {
        "score": sum(outcomes.values()) / len(outcomes) if outcomes else None,
        "successes": sum(outcomes.values()),
        "tasks": len(outcomes),
        "routing_failures": sorted(failures),
        "outcomes": outcomes,
        "route_assignment": route_tasks,
        "route_hash": read(route_dir / "complete.json").get("fingerprint"),
        "library_hash": summary.get("library_hash"),
        "test_dir": str(target),
    }


def mcnemar_exact(discordant_left, discordant_right):
    """Two-sided exact McNemar p-value for paired binary outcomes."""
    n = discordant_left + discordant_right
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(discordant_left, discordant_right) + 1))
    return min(1.0, 2.0 * tail / (2 ** n))


def paired(before, after):
    if set(before["outcomes"]) != set(after["outcomes"]):
        raise ValueError("Paired conditions do not cover the same test tasks")
    task_ids = sorted(before["outcomes"])

    def cells_for(ids):
        cells = {
            "correct_to_wrong": [],
            "wrong_to_correct": [],
            "both_correct": [],
            "both_wrong": [],
        }
        for task_id in ids:
            old, new = before["outcomes"][task_id], after["outcomes"][task_id]
            if old and new:
                key = "both_correct"
            elif old and not new:
                key = "correct_to_wrong"
            elif not old and new:
                key = "wrong_to_correct"
            else:
                key = "both_wrong"
            cells[key].append(task_id)
        return cells

    def stats(ids):
        cells = cells_for(ids)
        return {
            "tasks": len(ids),
            "cells": {key: {"count": len(value), "task_ids": value}
                      for key, value in cells.items()},
            "mcnemar": {
                "wrong_to_correct": len(cells["wrong_to_correct"]),
                "correct_to_wrong": len(cells["correct_to_wrong"]),
                "exact_two_sided_p": mcnemar_exact(
                    len(cells["wrong_to_correct"]), len(cells["correct_to_wrong"])
                ),
            },
        }

    cells = cells_for(task_ids)
    routed_ids = [
        task_id for task_id in task_ids
        if before["route_assignment"].get(task_id) is not None
        and after["route_assignment"].get(task_id) is not None
    ]
    same_route_ids = [
        task_id for task_id in routed_ids
        if before["route_assignment"].get(task_id)
        == after["route_assignment"].get(task_id)
    ]
    route_changes = {
        task_id: {
            "before": before["route_assignment"].get(task_id),
            "after": after["route_assignment"].get(task_id),
        }
        for task_id in task_ids
        if before["route_assignment"].get(task_id)
        != after["route_assignment"].get(task_id)
    }
    result = {
        "before_score": before["score"],
        "after_score": after["score"],
        "score_delta": after["score"] - before["score"],
        "cells": {key: {"count": len(value), "task_ids": value}
                  for key, value in cells.items()},
        "mcnemar": {
            "wrong_to_correct": len(cells["wrong_to_correct"]),
            "correct_to_wrong": len(cells["correct_to_wrong"]),
            "exact_two_sided_p": mcnemar_exact(
                len(cells["wrong_to_correct"]), len(cells["correct_to_wrong"])
            ),
        },
        "routed_only": stats(routed_ids),
        "same_route_only": stats(same_route_ids),
        "route_failure_task_ids": {
            "before": sorted(task_id for task_id in task_ids
                              if before["route_assignment"].get(task_id) is None),
            "after": sorted(task_id for task_id in task_ids
                             if after["route_assignment"].get(task_id) is None),
        },
        "route_changes": route_changes,
    }
    return result


def summarize(plan, root):
    validate_plan(plan)
    root = Path(root).resolve()
    rows = matrix_status(plan, root)["conditions"]
    incomplete = [row["condition"] for row in rows if row.get("status") != "test_complete"]
    if incomplete:
        raise ValueError("Cannot summarize incomplete conditions: " + ", ".join(incomplete))

    results = {}
    for condition in plan["conditions"]:
        name = condition["name"]
        results[name] = {
            benchmark: test_artifact(root / name, benchmark)
            for benchmark in plan["protocol"]["benchmarks"]
        }
    baseline = results["baseline"]
    comparisons = {}
    for condition in plan["conditions"]:
        if condition["name"] == "baseline":
            continue
        comparisons[condition["name"]] = {
            benchmark: paired(baseline[benchmark], results[condition["name"]][benchmark])
            for benchmark in plan["protocol"]["benchmarks"]
        }
    output = {
        "status": "complete",
        "protocol": plan["protocol"],
        "conditions": {
            name: {
                benchmark: {
                    key: value for key, value in artifact.items()
                    if key != "outcomes"
                }
                for benchmark, artifact in condition_result.items()
            }
            for name, condition_result in results.items()
        },
        "comparisons_vs_baseline": comparisons,
    }
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = summarize(validate_plan(read(args.matrix)), args.root)
    save(args.output, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
