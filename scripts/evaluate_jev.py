#!/usr/bin/env python3
"""Compare JEV's Skill success predictions with real validation episodes.

The script uses the initial Skill descriptions to freeze validation routing, then
measures each routed task once with the benchmark executor and once with JEV.
It is deliberately separate from L2 acceptance so calibration can be inspected
before enabling ``--acceptance-mode jev``.
"""

import argparse
import json
import os
from pathlib import Path

from omegaconf import OmegaConf

from skillexpand import schema as S
from skillexpand.evaluation import validation as VA
from skillexpand.evaluation.jev import JevClient, JevSkillScorer
from skillexpand.evaluation.jev import load_panel_records, score_actual_records
from skillexpand.evaluation.routing import FrozenRoutes
from skillexpand.persistence.store import SkillLibrary
from skillexpand.runtime import agent_factory as F


def load_plan(root, cfg):
    raw = json.loads((root / "split.json").read_text())
    assignment = {
        int(task_id): split
        for task_id, split in raw["assignment"].items()
    }
    mapping = json.loads((root / "task_skill_map.json").read_text())
    families = {}
    for task_id, skill_id in mapping.items():
        family = str(skill_id).split(".", 1)[-1]
        families.setdefault(family, []).append(int(task_id))
    return S.SplitPlan.make(assignment, cfg.benchmark.name, raw.get("seed", 42), families)


def load_skills(root, version, benchmark):
    if version == "initial":
        return tuple(S.from_dict(S.Skill, value)
                     for value in json.loads((root / "initial_skills.json").read_text()))
    library = SkillLibrary(root / "skills.jsonl", benchmark=benchmark)
    return tuple(library.head(family) for family in sorted(library.families))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skill-version", choices=("initial", "head"), default="head")
    parser.add_argument("--limit", type=int, help="Maximum validation tasks overall")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--jev-url", default=None)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument(
        "--actual-panel",
        type=Path,
        help="Existing per-task executor results (for example panel_scores.jsonl).",
    )
    parser.add_argument(
        "--routes-dir",
        type=Path,
        help="Existing frozen routes root. Reusing it avoids re-running the selector.",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")

    root = args.run_dir.resolve()
    cfg = OmegaConf.load(root / "config.json")
    os.environ.setdefault("EXPE_CONFIG_FILE", str(root / "config.json"))
    os.environ.setdefault("EXPE_TASK_FILE", str(cfg.benchmark.task_file))
    plan = load_plan(root, cfg)
    initial = tuple(S.from_dict(S.Skill, value)
                    for value in json.loads((root / "initial_skills.json").read_text()))
    skills = load_skills(root, args.skill_version, cfg.benchmark.name)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.routes_dir:
        routes = FrozenRoutes.load_existing(
            cfg, plan, initial, args.routes_dir.resolve(), S.SPLIT_VAL
        )
    else:
        routes = FrozenRoutes(cfg, plan, initial, args.output / "routes",
                              S.SPLIT_VAL, args.workers).run()
    task_ids = []
    for skill in skills:
        task_ids.extend(routes.groups.get(skill.skill_id, ()))
    task_ids = sorted(task_ids)
    if args.limit is not None:
        task_ids = task_ids[:args.limit]
    by_skill = {skill.skill_id: [task for task in task_ids
                                 if task in routes.groups.get(skill.skill_id, ())]
                for skill in skills}

    actual_records = load_panel_records(args.actual_panel) if args.actual_panel else None
    actual = None if actual_records is not None else VA.FixedSkillScorer(
        cfg, VA.ScoreCache(args.output / "actual-scores.jsonl"), routes, args.workers)
    jev = JevSkillScorer(
        cfg, routes, VA.ScoreCache(args.output / "jev-scores.jsonl"), args.workers,
        client=JevClient(args.jev_url, threshold=args.threshold),
    )
    rows = []
    missing_actual = {}
    for skill in skills:
        ids = by_skill[skill.skill_id]
        if not ids:
            continue
        if actual_records is None:
            actual_score = actual.score(skill, ids, f"val:{routes.fingerprint}:{skill.skill_id}")
            actual_by_task = {item.task_id: item.success for item in actual_score.outcomes}
            actual_ids = tuple(ids)
        else:
            actual_by_task, missing = score_actual_records(actual_records, skill, ids)
            actual_ids = tuple(task_id for task_id in ids if task_id in actual_by_task)
            if missing:
                missing_actual[skill.skill_id] = list(missing)
        if not actual_ids:
            continue
        actual_successes = sum(actual_by_task.values())
        jev_score = jev.score(skill, ids, f"val:{routes.fingerprint}:{skill.skill_id}")
        jev_by_task = jev_score.by_task()
        confusion = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
        predictions = []
        for task_id in actual_ids:
            truth = bool(actual_by_task[task_id])
            predicted = bool(jev_by_task[task_id]["predicted_success"])
            key = ("tp" if truth and predicted else "tn" if not truth and not predicted
                   else "fp" if predicted else "fn")
            confusion[key] += 1
            predictions.append({"task_id": task_id, "actual_success": truth,
                                "jev_probability": jev_by_task[task_id]["probability_true"],
                                "jev_predicted_success": predicted})
        rows.append({"skill_id": skill.skill_id, "skill_key": skill.key,
                     "tasks": len(actual_ids), "routed_tasks": len(ids),
                     "actual_successes": actual_successes,
                     "actual_score": actual_successes / len(actual_ids),
                     "jev_predicted_successes": jev_score.successes,
                     "jev_predicted_score": jev_score.score,
                     "jev_mean_probability": jev_score.mean_probability,
                     "accuracy": sum(v == k for p in predictions
                                     for v, k in [(p["actual_success"], p["jev_predicted_success"])]) / len(actual_ids),
                     "confusion": confusion, "predictions": predictions})
    total = sum(row["tasks"] for row in rows)
    correct = sum(row["accuracy"] * row["tasks"] for row in rows)
    output = {"protocol": "jev-validation-calibration-v1", "benchmark": cfg.benchmark.name,
              "split": "val", "skill_version": args.skill_version,
              "tasks": total, "routed_tasks": len(task_ids),
              "missing_actual_tasks": sum(len(v) for v in missing_actual.values()),
              "missing_actual_by_skill": missing_actual,
              "actual_source": str(args.actual_panel) if args.actual_panel else "fresh_executor",
              "accuracy": correct / total if total else None,
              "skills": rows, "routes_fingerprint": routes.fingerprint}
    (args.output / "summary.json").write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(json.dumps({k: output[k] for k in ("benchmark", "split", "skill_version", "tasks", "accuracy")}, indent=2))


if __name__ == "__main__":
    main()
