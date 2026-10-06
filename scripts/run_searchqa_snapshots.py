#!/usr/bin/env python3
"""Evaluate cold-start, Evolve-1, and Evolve-2 Skills on frozen SearchQA routes."""
import json
import os
import runpy
import sys
import time
from pathlib import Path


LABELS = ("cold-start", "evolve-1", "evolve-2")


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def read(path):
    return json.loads(Path(path).read_text())


def choose_snapshots(S, initial, library, run):
    snapshots = {"cold-start": list(initial)}
    for n in (1, 2):
        summary = read(run / "evolution" / f"round-{n}" / "summary.json")
        if summary.get("status") != "complete":
            raise ValueError(f"round-{n} summary is not complete")
        snapshots[f"evolve-{n}"] = [
            library.get(key) for _, key in sorted(summary["skills"].items())
        ]
        if any(skill is None for skill in snapshots[f"evolve-{n}"]):
            raise ValueError(f"round-{n} references a missing Skill revision")
        if {skill.skill_id for skill in snapshots[f"evolve-{n}"]} != {
            skill.skill_id for skill in initial
        }:
            raise ValueError(f"round-{n} changes the Skill family set")
        descriptions = {skill.skill_id: skill.description for skill in initial}
        if any(skill.description != descriptions[skill.skill_id]
               for skill in snapshots[f"evolve-{n}"]):
            raise ValueError(f"round-{n} changes frozen routing descriptions")
    return snapshots


def paired(results, S):
    outcomes = {}
    for label, result in results.items():
        target = Path(result["target"])
        rows = [json.loads(line) for line in (target / "scores.jsonl").read_text().splitlines()]
        by_task = {row["task_id"]: bool(row["success"]) for row in rows}
        if len(by_task) != len(rows):
            raise ValueError(f"duplicate score task in {label}")
        for task in result["routing_failures"]:
            if task in by_task:
                raise ValueError(f"routing failure also scored: {label}/{task}")
            by_task[task] = False
        if len(by_task) != result["tasks"]:
            raise ValueError(f"incomplete paired coverage in {label}")
        outcomes[label] = by_task
    output = {}
    for before, after in (("cold-start", "evolve-1"), ("evolve-1", "evolve-2"),
                          ("cold-start", "evolve-2")):
        a, b = outcomes[before], outcomes[after]
        if set(a) != set(b):
            raise ValueError("snapshot task populations differ")
        cells = {"correct_to_wrong": [], "wrong_to_correct": [],
                 "both_correct": [], "both_wrong": []}
        for task in sorted(a):
            if a[task] and b[task]:
                key = "both_correct"
            elif a[task] and not b[task]:
                key = "correct_to_wrong"
            elif not a[task] and b[task]:
                key = "wrong_to_correct"
            else:
                key = "both_wrong"
            cells[key].append(task)
        output[f"{before}->{after}"] = {
            key: {"count": len(value), "task_ids": value}
            for key, value in cells.items()
        }
    return output


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_searchqa_snapshots.py CONDITION_ROOT")
    root = Path(sys.argv[1]).resolve()
    campaign = runpy.run_path(str(root / "code" / "run_campaign.py"))
    manifest = campaign["verify"](root)
    run = root / "full" / "searchqa" / "run"
    if len(list((run / "test").glob("*/summary.json"))) != 1:
        raise ValueError("Run the canonical SearchQA Evolve-2 test before snapshots")
    env = campaign["environment"](root)
    os.environ.update(env)
    os.environ["EXPE_CONFIG_FILE"] = str(run / "config.json")
    os.environ["EXPE_TASK_FILE"] = str(read(run / "config.json")["benchmark"]["task_file"])
    for path in reversed(env["PYTHONPATH"].split(os.pathsep)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from omegaconf import OmegaConf
    from skillexpand import schema as S
    from skillexpand.evaluation.audit import audit_test
    from skillexpand.evaluation.routing import FrozenRoutes
    from skillexpand.evaluation.validation import FixedSkillScorer, ScoreCache, library_fingerprint
    from skillexpand.persistence.artifacts import (code_signature, load_cold_start,
                                                    provider_signature)
    from skillexpand.persistence.store import SkillLibrary

    cfg, plan, initial, _ = load_cold_start(run)
    library = SkillLibrary(run / "skills.jsonl", benchmark=plan.benchmark)
    snapshots = choose_snapshots(S, list(initial), library, run)
    route_root = run / "routes"
    routes = FrozenRoutes.load_existing(cfg, plan, initial, route_root, S.SPLIT_TEST)
    workers = manifest["concurrency"]["searchqa"]["test_workers"]
    results = {}
    for label in LABELS:
        skills = snapshots[label]
        fingerprint = library_fingerprint(skills)
        target = run / "test-snapshots" / label / fingerprint
        target.mkdir(parents=True, exist_ok=True)
        save(target / "library.json", [S.to_dict(skill) for skill in skills])
        save(target / "protocol.json", {
            "code": code_signature(), "provider": provider_signature(),
            "config": OmegaConf.to_container(cfg, resolve=True),
            "routing_reference": [S.to_dict(skill) for skill in initial],
            "snapshot": label,
        })
        if (target / "summary.json").exists():
            audit = audit_test(run, target)
            summary = read(target / "summary.json")
        else:
            scorer = FixedSkillScorer(cfg, ScoreCache(target / "scores.jsonl"), routes, workers)
            save(target / "score_protocol.json", {"hash": scorer.protocol_hash})
            per_skill = {}
            for skill in skills:
                result = scorer.score(skill, routes.groups[skill.skill_id],
                                      f"test:{routes.fingerprint}:{skill.skill_id}")
                per_skill[skill.skill_id] = {
                    "tasks": result.n, "successes": result.successes, "score": result.score
                }
                save(target / "skills" / f"{skill.skill_id}.json", per_skill[skill.skill_id])
            total = len(plan.tasks_in(S.SPLIT_TEST))
            summary = {
                "status": "complete", "benchmark": "searchqa", "split": "test",
                "evolution_snapshot": label, "library_hash": fingerprint,
                "routing_reference": "initial_skills", "tasks": total,
                "successes": sum(item["successes"] for item in per_skill.values()),
                "score": sum(item["successes"] for item in per_skill.values()) / total,
                "per_skill": per_skill, "routing_failures": list(routes.failed_task_ids),
            }
            save(target / "summary.json", summary)
            audit = audit_test(run, target)
        save(target / "audit.json", audit)
        usage_audit = campaign["audit_usage_ledgers"](target)
        save(target / "usage-audit.json", usage_audit)
        if usage_audit.get("failed_requests"):
            raise ValueError(f"snapshot usage contains failed requests: {label}")
        results[label] = {"target": str(target), **summary,
                          "audit": audit, "usage_audit": usage_audit}
        save(run / "test-snapshots.json", results)
    paired_results = paired(results, S)
    save(run / "test-snapshots-paired.json", paired_results)
    save(run / "test-snapshots-complete.json", {
        "status": "complete", "benchmark": "searchqa", "snapshots": results,
        "paired": paired_results, "finished": time.time(),
    })
    print(json.dumps({"status": "complete", "snapshots": results,
                      "paired": paired_results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
