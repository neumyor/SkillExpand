#!/usr/bin/env python3
"""Plan, merge, and gate a resumable E1 progressive repair run.

The original E1 directory is immutable.  This utility computes task/attempt
differences against the frozen 89-task manifest, writes a repair ledger, and
only permits the formal downstream gate after all route/card/raw evidence is
present.  Provider and Harbor exceptions remain annotated; they are never
silently converted into rollout reward=0.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from collections import Counter
from pathlib import Path


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _task_names(task_file: Path) -> list[str]:
    rows = _read(task_file)
    return [str(row["task_name"] if "task_name" in row else row["name"]) for row in rows]


def _task_ids(task_file: Path) -> list[int]:
    return list(range(len(_read(task_file))))


def _result_rows(root: Path, task_names: set[str]) -> list[dict]:
    grouped: dict[str, list[tuple[Path, dict, Path]]] = {}
    for path in sorted(root.glob("**/result.json")):
        result = _read(path)
        task = result.get("task_name") or path.parent.name.split("__", 1)[0]
        if task not in task_names:
            continue
        trajectory = path.parent / "agent" / "trajectory.json"
        if not trajectory.is_file() and any(path.parent.glob("*/agent/trajectory.json")):
            continue
        grouped.setdefault(task, []).append((path, result, trajectory))
    rows = []
    for task, entries in sorted(grouped.items()):
        # Harbor's leaf result files do not carry an attempt index.  The frozen
        # source contract is three attempts per task; assign stable indices in
        # path order and reject overflow instead of silently reusing retries.
        for index, (path, result, trajectory) in enumerate(entries, 1):
            if index > 3:
                continue
            attempt = result.get("attempt_index") or index
            if not isinstance(attempt, int):
                attempt = index
            rows.append({
                "task_name": task,
                "attempt_index": int(attempt),
                "result_path": str(path),
                "trajectory_path": str(trajectory),
                "trajectory_present": trajectory.is_file(),
                "exception": result.get("exception_info") or {},
                "source_model": (result.get("config", {}).get("agent", {})
                                  .get("model_name")),
                "key": (task, int(attempt)),
            })
    return rows


def _route_ids(root: Path, task_count: int) -> set[int]:
    ids = set()
    for path in root.glob("routes/train/tasks/*.json"):
        if path.stem.isdigit() and 0 <= int(path.stem) < task_count:
            ids.add(int(path.stem))
    return ids


def _card_ids(root: Path, task_count: int) -> set[int]:
    ids = set()
    for path in root.glob("discovery/results/*.json"):
        try:
            value = _read(path)
        except (OSError, json.JSONDecodeError):
            continue
        raw = value.get("task_id", path.stem)
        if isinstance(raw, dict):
            raw = path.stem
        if str(raw).isdigit() and 0 <= int(raw) < task_count:
            ids.add(int(raw))
    return ids


def build_report(original: Path, task_file: Path, source: Path) -> dict:
    tasks = _task_names(task_file)
    task_ids = _task_ids(task_file)
    task_set = set(tasks)
    status = _read(original / "status.json") if (original / "status.json").is_file() else {}
    route_ids = _route_ids(original, len(tasks))
    card_ids = _card_ids(original, len(tasks))
    rows = _result_rows(source, task_set)
    keys = [row["key"] for row in rows if row["key"] is not None]
    counts = Counter(task for task, _ in keys)
    duplicate_keys = sorted(key for key, count in Counter(keys).items() if count > 1)
    expected_keys = {(task, attempt) for task in tasks for attempt in (1, 2, 3)}
    present_keys = set(keys)
    missing_keys = sorted(expected_keys - present_keys)
    return {
        "expected_tasks": len(tasks),
        "expected_rollouts": len(expected_keys),
        "status_completed_routes": status.get("completed_routes"),
        "filesystem_routes": len(route_ids),
        "route_task_ids": sorted(route_ids),
        "missing_route_task_ids": sorted(set(task_ids) - route_ids),
        "card_task_ids": sorted(card_ids),
        "missing_card_task_ids": sorted(set(task_ids) - card_ids),
        "existing_result_records": len(rows),
        "existing_unique_attempts": len(present_keys),
        "attempt_counts": dict(sorted(counts.items())),
        "missing_attempts": [{"task_name": task, "attempt_index": attempt} for task, attempt in missing_keys],
        "duplicate_attempt_keys": [list(key) for key in duplicate_keys],
        "raw_records_with_trajectory": sum(row["trajectory_present"] for row in rows),
        "source_root": str(source),
        "original_run": str(original),
    }


def write_plan(args: argparse.Namespace) -> int:
    report = build_report(args.original.resolve(), args.task_file.resolve(), args.source.resolve())
    root = args.repair.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "repair_plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (root / "status.json").write_text(json.dumps({"stage": "E1", "status": "repair_planned", "report": report}, ensure_ascii=False, indent=2) + "\n")
    (root / "audit.json").write_text(json.dumps({
        "stage": "E1", "status": "repair_planned", "original_run_immutable": True,
        "route_missing_count": len(report["missing_route_task_ids"]),
        "card_missing_count": len(report["missing_card_task_ids"]),
        "missing_attempt_count": len(report["missing_attempts"]),
        "duplicate_attempt_keys": report["duplicate_attempt_keys"],
    }, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "status": "repair_planned",
        "missing_route_count": len(report["missing_route_task_ids"]),
        "missing_route_task_ids": report["missing_route_task_ids"],
        "missing_card_count": len(report["missing_card_task_ids"]),
        "missing_attempt_count": len(report["missing_attempts"]),
        "status_completed_routes": report["status_completed_routes"],
        "filesystem_routes": report["filesystem_routes"],
    }, ensure_ascii=False, indent=2))
    return 0


def merge(args: argparse.Namespace) -> int:
    root = args.repair.resolve()
    report = _read(root / "repair_plan.json")
    task_file = args.task_file.resolve()
    task_count = len(_task_ids(task_file))
    rows = _result_rows(args.source.resolve(), set(_task_names(task_file)))
    by_key = {}
    duplicates = []
    for row in rows:
        key = row["key"]
        if key is None:
            continue
        if key in by_key:
            duplicates.append(list(key))
            continue
        by_key[key] = row
    expected = {(task, attempt) for task in _task_names(args.task_file.resolve()) for attempt in (1, 2, 3)}
    missing = sorted(expected - set(by_key))
    route_ids = _route_ids(root, task_count)
    card_ids = _card_ids(args.original.resolve(), task_count)
    route_complete = route_ids == set(range(task_count))
    card_complete = card_ids == set(range(task_count))
    ledger = {
        "stage": "E1", "status": "complete" if route_complete and card_complete and not missing and not duplicates else "needs_attention",
        "records": len(by_key), "expected_records": len(expected),
        "route_tasks": len(route_ids), "expected_route_tasks": task_count,
        "card_tasks": len(card_ids), "expected_card_tasks": task_count,
        "missing_attempts": [{"task_name": t, "attempt_index": a} for t, a in missing],
        "duplicate_attempt_keys": duplicates,
        "records": sorted(by_key.values(), key=lambda row: (row["task_name"], row["attempt_index"])),
    }
    (root / "repair_ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    state = "ready_for_gate" if route_complete and card_complete and not missing and not duplicates else "needs_attention"
    coverage = {"routes": len(route_ids), "cards": len(card_ids), "raw_records": len(by_key),
                "expected_tasks": task_count, "expected_raw_records": len(expected),
                "missing_raw_records": len(missing)}
    (root / "input_coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")
    (root / "status.json").write_text(json.dumps({"stage": "E1", "status": state, "coverage": coverage, "report": report}, ensure_ascii=False, indent=2) + "\n")
    (root / "audit.json").write_text(json.dumps({"stage": "E1", "status": state, "dedup_key": ["task_name", "attempt_index"], "missing_attempt_count": len(missing), "duplicate_attempt_keys": duplicates, "route_complete": route_complete, "card_complete": card_complete, "source_root": str(args.source.resolve()), "original_run_immutable": True}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": state, **coverage, "duplicates": len(duplicates)}, indent=2))
    return 0 if state == "ready_for_gate" else 2


def launch_routes(args: argparse.Namespace) -> int:
    """Copy immutable cold-start/route inputs, then retry only missing selectors."""
    from skillexpand import schema as S
    from skillexpand.evaluation.routing import FrozenRoutes
    from skillexpand.persistence.artifacts import load_cold_start
    from skillexpand.runtime import agent_factory as F
    from skillexpand.runtime.llm_relay import relay_from_env

    original = args.original.resolve()
    root = args.repair.resolve()
    report = _read(root / "repair_plan.json")
    if report["missing_card_task_ids"]:
        raise RuntimeError("card coverage is incomplete; route-only repair is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "cold_start_complete.json").is_file():
        # Progressive cold-starts intentionally have no clusters/task map.
        # Copy only the frozen progressive inputs instead of using the legacy
        # importer, which requires non-progressive artifacts.
        for name in ("config.json", "split.json", "manifest.json",
                     "initial_skills.json", "cold_start_complete.json"):
            shutil.copyfile(original / name, root / name)
        for name in ("discovery", "skills.jsonl"):
            source_path = original / name
            target_path = root / name
            if source_path.is_dir():
                shutil.copytree(source_path, target_path, dirs_exist_ok=True)
            elif source_path.is_file():
                shutil.copyfile(source_path, target_path)
    source_routes = original / "routes" / "train"
    target_routes = root / "routes" / "train"
    if not target_routes.exists():
        target_routes.mkdir(parents=True)
        for name in ("tasks", "errors", "usage"):
            source_dir = source_routes / name
            if source_dir.exists():
                shutil.copytree(source_dir, target_routes / name)
    cfg, plan, skills, _ = load_cold_start(root)
    relay = relay_from_env()
    base_url = relay.start()
    previous = {key: os.environ.get(key) for key in (
        "EXPE_LLM_BASE_URL", "OPENAI_API_BASE", "MODEL_API_BASE",
        "EXPE_LLM_RELAY_REQUIRED", "EXPE_CONFIG_FILE")}
    try:
        os.environ["EXPE_LLM_BASE_URL"] = base_url
        os.environ["OPENAI_API_BASE"] = base_url
        os.environ["MODEL_API_BASE"] = base_url
        os.environ["EXPE_LLM_RELAY_REQUIRED"] = "1"
        os.environ["EXPE_CONFIG_FILE"] = str(root / "config.json")
        value = json.loads((root / "config.json").read_text())
        value.setdefault("benchmark", {}).setdefault("rollout", {})["relay_base_url"] = base_url
        value["benchmark"]["rollout"]["llm_transport"] = "tencent_e2b_relay"
        value["benchmark"]["rollout"]["direct_provider_fallback"] = False
        (root / "config.json").write_text(json.dumps(value, indent=2) + "\n")
        (root / "status.json").write_text(json.dumps({
            "stage": "E1", "status": "running_selector_repair",
            "missing_route_task_ids": report["missing_route_task_ids"],
            "workers": 100, "transport": "tencent_e2b_relay",
            "task_sandbox_persistence": False, "started_at": time.time(),
        }, indent=2) + "\n")
        routes = FrozenRoutes(cfg, plan, skills, root / "routes", S.SPLIT_TRAIN, 100).run()
        (root / "route_manifest.json").write_text(json.dumps({
            "stage": "E1", "status": "complete", "tasks": len(routes.records),
            "missing_before": report["missing_route_task_ids"],
            "repaired": sorted(int(t) for t in report["missing_route_task_ids"]),
            "transport": "tencent_e2b_relay", "workers": 100,
        }, indent=2) + "\n")
        (root / "audit.json").write_text(json.dumps({
            "stage": "E1", "status": "route_repair_complete",
            "route_tasks": len(routes.records),
            "expected_route_tasks": len(_task_ids(args.task_file.resolve())),
            "dedup_key": ["task_id"],
            "source_catalog": "imported immutable cold-start v0",
            "original_run_immutable": True,
        }, indent=2) + "\n")
        (root / "status.json").write_text(json.dumps({
            "stage": "E1", "status": "selector_repair_complete",
            "route_tasks": len(routes.records), "expected_route_tasks": len(_task_ids(args.task_file.resolve())),
            "next": "reuse existing 89 cards; planner/editor/reviewer/gate remains locked until final audit",
        }, indent=2) + "\n")
        print(json.dumps({"status": "selector_repair_complete", "route_tasks": len(routes.records)}, indent=2))
        return 0
    finally:
        relay.close()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "launch-routes", "merge"))
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--repair", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "plan":
        return write_plan(args)
    if args.action == "launch-routes":
        return launch_routes(args)
    return merge(args)


if __name__ == "__main__":
    raise SystemExit(main())
