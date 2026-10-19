"""Run the 3-task, 4-arm Skill package engineering smoke.

This driver is deliberately independent of the 89-task aligned runner. It
creates one result directory per task/arm, so an interrupted smoke resumes
without reusing a different arm's artifacts.
"""
import argparse
import json
import os
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from skillexpand import schema as S
from skillexpand.benchmarks.terminalbench import harbor_rollout
from skillexpand.skill_package import materialize_skill, render_skill_md

TASKS = ("polyglot-rust-c", "db-wal-recovery", "feal-differential-cryptanalysis")
ARMS = ("bare", "historical", "inline", "package")


def load_tasks(path):
    payload = json.loads(Path(path).read_text())
    rows = payload.get("tasks", payload) if isinstance(payload, dict) else payload
    selected = [row for row in rows if row.get("task_name") in TASKS]
    if {row["task_name"] for row in selected} != set(TASKS):
        raise ValueError("Smoke task file does not contain all three target tasks")
    return [next(row for row in selected if row["task_name"] == name) for name in TASKS]


def smoke_skill(task_name, *, package=False):
    return S.Skill(
        skill_id=f"smoke.{task_name}", family_id=f"smoke.{task_name}", version=0,
        name=task_name, description="Engineering smoke package; do not use for scoring.",
        body="Inspect the task files, implement the requested deliverable, and run the available checks.",
        resources={"references/README.txt": "Smoke-only package resource.\n",
                   "scripts/inspect.sh": "#!/bin/sh\nprintf '%s\\n' smoke-resource-used\n"},
        package_mode=package,
    )


class ConfigSection(dict):
    def __getattr__(self, name):
        return self[name]


def config(task_file, runner_script):
    return ConfigSection(benchmark=ConfigSection(
        task_file=str(task_file),
        rollout={"runner_script": str(runner_script),
                 "llm_transport": "tencent_e2b_relay",
                 "provider_base_url": "https://llm-center.modelbest.co/v1"},
    ))


def run(args):
    tasks = load_tasks(args.task_file)
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    task_file = root / 'tasks.json'
    task_file.write_text(json.dumps(tasks, indent=2) + '\n')
    cfg = config(task_file, args.runner_script)
    manifest = {"format": "tb21-package-smoke-v1", "tasks": list(TASKS), "arms": list(ARMS),
                "attempts": 1, "status": "running"}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.validate_only:
        return
    def run_slot(item):
        task_index, task, arm = item
        slot = root / task["task_name"] / arm
        done = slot / "smoke.json"
        if done.exists():
            prior = json.loads(done.read_text())
            if valid_outcome(prior):
                return
            history = slot / f'smoke.before-{os.getpid()}.json'
            history.write_text(json.dumps(prior, indent=2) + '\n')
        slot.mkdir(parents=True, exist_ok=True)
        skill = None if arm == "bare" else smoke_skill(task["task_name"], package=arm == "package")
        if skill is not None:
            package_root = slot / "skills" / task["task_name"]
            if arm == "inline":
                package_root.mkdir(parents=True, exist_ok=True)
                inline = render_skill_md(skill) + "\n".join(
                    f"\n## Resource: {name}\n\n```text\n{content.rstrip()}\n```"
                    for name, content in sorted(skill.resources.items())
                ) + "\n"
                skill = replace(skill, body=inline, resources={}, package_mode=False)
            else:
                materialize_skill(skill, package_root, package=True)
        try:
            rollout, runtime = harbor_rollout(cfg, task_index, skill, 1, slot, evolution_round=0)
            record = {"status": "unresolved", "task_name": task["task_name"], "arm": arm,
                      "rollout": rollout, "runtime": runtime}
            if valid_outcome(record):
                record['status'] = 'completed'
        except Exception as exc:
            record = {"status": "infrastructure_error", "task_name": task["task_name"],
                      "arm": arm, "error_type": type(exc).__name__, "error": str(exc)[:2000]}
        done.write_text(json.dumps(record, indent=2) + "\n")
        if record["status"] != "completed" and args.fail_fast:
            raise RuntimeError(record["error"])
    items = [(i, task, arm) for i, task in enumerate(tasks) for arm in ARMS]
    if args.slots:
        items = [item for item in items if f'{item[1]["task_name"]}/{item[2]}' in args.slots]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_slot, item) for item in items]
        for future in as_completed(futures):
            future.result()
    records = [json.loads(path.read_text()) for task in TASKS for path in (root / task).glob("*/smoke.json")]
    manifest.update(status="complete" if len(records) == 12 and all(valid_outcome(r) for r in records) else "needs_attention",
                    completed=len(records), healthy=sum(valid_outcome(r) for r in records))
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def valid_outcome(record):
    trials = record.get('rollout', {}).get('trials', [])
    return (len(trials) == 1 and not isinstance(trials[0].get('reward'), bool)
            and trials[0].get('reward') in (0, 1)
            and trials[0].get('exception_type') in (None, 'AgentTimeoutError'))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--runner-script", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument('--slots', nargs='+')
    run(parser.parse_args())
