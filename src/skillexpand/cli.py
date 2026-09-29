"""Complete L1 cold start, then batch-local L2 card review, with independent test evaluation."""

import argparse
import json
import os
from pathlib import Path
from omegaconf import OmegaConf
from skillexpand.runtime import agent_factory as F
from skillexpand import schema as S
from skillexpand.evaluation import splits as SP
from skillexpand.l2 import loop as L
from skillexpand.l1 import cold_start as C
from skillexpand.persistence import store as ST
from skillexpand.evaluation import validation as VA
from skillexpand.persistence.artifacts import load_cold_start
from skillexpand.persistence.artifacts import import_cold_start
from skillexpand.persistence.artifacts import code_signature
from skillexpand.persistence.artifacts import provider_signature
from skillexpand.evaluation.routing import FrozenRoutes
from skillexpand.l1.runner import save


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--benchmark", default="searchqa")
    p.add_argument("--task-file")
    p.add_argument("--run-dir", required=True)
    p.add_argument(
        "--cold-start-dir",
        help="Import completed cold-start inputs into this run; never rerun L1",
    )
    p.add_argument(
        "--phase",
        choices=("cold-start", "l2", "evolve", "test", "all"),
        default="cold-start",
    )
    p.add_argument("--split-file")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--cold-start-workers", type=int, default=8,
        help="Concurrent train-task L1 units during cold start"
    )
    p.add_argument(
        "--family-discovery-workers", type=int, default=8,
        help="Concurrent capability-tag and family-assignment requests"
    )
    p.add_argument("--autonomous-attempts", type=int, default=4)
    p.add_argument("--supervised-attempts", type=int, default=1,
                   help="Maximum supervised repair trials after autonomous attempts")
    p.add_argument("--no-supervised-repair", action="store_true")
    p.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Train cards per serial L2 proposal; tails included",
    )
    p.add_argument(
        "--candidate-count", type=int, default=3,
        help="Maximum candidate bodies independently reviewed on each train-card batch",
    )
    p.add_argument("--evolve-rounds", type=int, default=1,
        help="Number of Skill-aware L1 -> L2 evolution rounds")
    p.add_argument("--skill-edit-mode", choices=("rewrite", "structured"),
        default="rewrite", help="Rewrite complete Skill bodies or apply one structured rule edit")
    p.add_argument("--acceptance-mode", choices=("predicted", "empirical", "jev"),
        default="predicted", help="Accept by card review, paired execution, or JEV validation")
    p.add_argument("--predicted-review-scope", choices=("val", "train_cards"),
        default="val", help="Evidence scope for predicted acceptance")
    p.add_argument("--evolve-l1-workers", type=int, default=8,
        help="Concurrent train tasks during each Skill-aware L1 round")
    p.add_argument("--l2-review-workers", type=int, default=8,
        help="Concurrent per-card LLM reviews within each L2 batch")
    p.add_argument(
        "--test-workers",
        type=int,
        default=4,
        help="Concurrent test routing and evaluation tasks",
    )
    p.add_argument('--l1-model', help='LLM used by the task execution agent')
    p.add_argument('--cold-start-model', help='LLM used by cold-start discovery and Skill synthesis')
    p.add_argument('--l2-planner-model', help='LLM used to propose L2 hypotheses')
    p.add_argument('--l2-editor-model', help='LLM used to materialize rewrite-mode candidates')
    p.add_argument('--l2-reviewer-model', help='LLM used by per-card L2 reviewers')
    p.add_argument('--selector-model', help='LLM used to route validation/test tasks')
    p.add_argument("--resume", action="store_true")
    p.add_argument("--show-plan", action="store_true")
    return p


def make_plan(cfg, args, root):
    count = len(F.task_table(cfg))
    if args.split_file:
        raw = json.loads(Path(args.split_file).read_text())
        assignment = {int(k): v for k, v in raw.get("assignment", raw).items()}
        plan = S.SplitPlan.make(assignment, cfg.benchmark.name, args.seed)
    elif (root / "split.json").exists():
        plan = C.read_split(root / "split.json")
    else:
        generated = SP.build_split_plan(
            {"tasks": list(range(count))}, benchmark=cfg.benchmark.name, seed=args.seed
        )
        plan = S.SplitPlan.make(generated.assignment, cfg.benchmark.name, args.seed)
    if (
        set(plan.assignment) != set(range(count))
        or plan.benchmark != cfg.benchmark.name
    ):
        raise ValueError("Split must cover exactly this benchmark task table")
    if any(not plan.tasks_in(split) for split in S.SPLITS):
        raise ValueError("train, val and test must all be nonempty")
    return plan


def apply_model_overrides(cfg, args):
    """Freeze independent model names for executor and reasoning roles."""
    defaults = {
        'l1_executor': str(cfg.agent.llm),
        'cold_start': str(cfg.agent.llm),
        'l2_planner': str(cfg.agent.llm),
        'l2_editor': str(cfg.agent.llm),
        'l2_reviewer': str(cfg.agent.llm),
        'selector': str(cfg.agent.llm),
    }
    existing = cfg.get('models', {})
    for key in defaults:
        if existing and existing.get(key):
            defaults[key] = str(existing[key])
    overrides = {
        'l1_executor': args.l1_model,
        'cold_start': args.cold_start_model,
        'l2_planner': args.l2_planner_model,
        'l2_editor': args.l2_editor_model,
        'l2_reviewer': args.l2_reviewer_model,
        'selector': args.selector_model,
    }
    for key, value in overrides.items():
        if value:
            defaults[key] = value
    cfg.models = OmegaConf.create(defaults)
    cfg.agent.llm = defaults['l1_executor']
    return cfg


def load_clustered_plan(root, plan):
    from skillexpand.l1.family_discovery import load_family_plan

    if not (root / "cold_start_complete.json").exists():
        raise ValueError("Cold start is incomplete")
    clusters = load_family_plan(root / "clusters.json", benchmark=plan.benchmark)
    mapping = json.loads((root / "task_skill_map.json").read_text())
    expected = {
        str(t): f"{plan.benchmark}.{f}" for t, f in clusters.task_to_family.items()
    }
    if mapping != expected or set(clusters.task_to_family) != set(
        plan.tasks_in(S.SPLIT_TRAIN)
    ):
        raise ValueError("Train mapping integrity failure")
    initial = json.loads((root / "initial_skills.json").read_text())
    complete = json.loads((root / "cold_start_complete.json").read_text())
    if complete["mapping_hash"] != S.content_hash(mapping) or complete[
        "initial_skills_hash"
    ] != S.content_hash(initial):
        raise ValueError("Cold-start artifact hash mismatch")
    return S.SplitPlan.make(
        plan.assignment, plan.benchmark, plan.seed, clusters.families_index
    )


def test_evaluate(cfg, plan, root, test_workers):
    with L.RunLock(root / 'run.pid'):
        return _test_evaluate(cfg, plan, root, test_workers)


def _test_evaluate(cfg, plan, root, test_workers):
    if any((root / 'evolution').glob('round-*/input.json')):
        from skillexpand.l2.audit import audit_round
        status = json.loads((root / 'summary.json').read_text())
        if status.get('status') != 'complete' or not status.get('latest_evolution_round'):
            raise ValueError('Complete and audit evolution before test evaluation')
        for index in range(1, status['latest_evolution_round'] + 1):
            audit_round(root, index)
    _, _, initial, _ = load_cold_start(root)
    library = ST.SkillLibrary(root / "skills.jsonl", benchmark=plan.benchmark)
    skills = tuple(library.head(f) for f in sorted(library.families))
    if any((root / 'evolution').glob('round-*/input.json')) and (
            {s.skill_id: s.key for s in skills} != status['skills']):
        raise ValueError('Skill library differs from completed evolution output')
    target = root / "test" / VA.library_fingerprint(skills)
    C.freeze(target / "library.json", [S.to_dict(s) for s in skills])
    C.freeze(
        target / "protocol.json",
        {
            "code": code_signature(),
            "provider": provider_signature(),
            "config": OmegaConf.to_container(cfg, resolve=True),
            "routing_reference": [S.to_dict(s) for s in initial],
        },
    )
    # Test questions/results are first accessed here. The initial descriptions are
    # the immutable routing reference; L2 never executes val or test tasks.
    routes = FrozenRoutes(
        cfg, plan, initial, root / "routes", S.SPLIT_TEST, test_workers
    ).run()
    scorer = VA.FixedSkillScorer(
        cfg, VA.ScoreCache(target / "scores.jsonl"), routes, test_workers
    )
    C.freeze(target / 'score_protocol.json', {'hash': scorer.protocol_hash})
    per_skill = {}
    for skill in skills:
        result = scorer.score(
            skill,
            routes.groups[skill.skill_id],
            f"test:{routes.fingerprint}:{skill.skill_id}",
        )
        per_skill[skill.skill_id] = {
            "tasks": result.n,
            "successes": result.successes,
            "score": result.score,
        }
        save(target / "skills" / (skill.skill_id + ".json"), per_skill[skill.skill_id])
    successes = sum(r["successes"] for r in per_skill.values())
    n = len(plan.tasks_in(S.SPLIT_TEST))
    summary = {
        "split": "test",
        "library_hash": VA.library_fingerprint(skills),
        "routing_reference": "initial_skills",
        "tasks": n,
        "successes": successes,
        "score": successes / n if n else None,
        "per_skill": per_skill,
        "routing_failures": list(routes.failed_task_ids),
    }
    save(target / "summary.json", summary)
    from skillexpand.evaluation.audit import audit_test
    save(target / 'audit.json', audit_test(root, target))
    return summary


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = Path(args.run_dir).resolve()
    if any(
        not 1 <= n <= 256
        for n in (args.cold_start_workers, args.family_discovery_workers,
                  args.evolve_l1_workers, args.l2_review_workers, args.test_workers)
    ):
        raise ValueError("Worker counts must be between 1 and 256")
    if args.cold_start_dir and args.phase == "cold-start":
        raise ValueError("Use --phase l2, all or test with --cold-start-dir")
    source = Path(args.cold_start_dir).resolve() if args.cold_start_dir else root
    completed = (source / "cold_start_complete.json").exists()
    if completed:
        cfg, plan, _, _ = load_cold_start(source)
        if (
            args.task_file
            and Path(args.task_file).resolve()
            != Path(cfg.benchmark.task_file).resolve()
        ):
            raise ValueError("Task file differs from completed cold start")
        if args.split_file:
            requested = make_plan(cfg, args, source)
            if requested.assignment != plan.assignment:
                raise ValueError("Split differs from completed cold start")
    else:
        if args.cold_start_dir or args.phase in ("l2", "evolve", "test"):
            raise ValueError("L2/test requires a completed cold start")
        if args.task_file:
            os.environ["EXPE_TASK_FILE"] = str(Path(args.task_file).resolve())
        cfg = (
            OmegaConf.load(root / "config.json")
            if args.resume and (root / "config.json").exists()
            else F.load_config(args.benchmark)
        )
        cfg.benchmark.task_file = str(Path(cfg.benchmark.task_file).resolve())
        plan = make_plan(cfg, args, root)
    if cfg.benchmark.name != args.benchmark:
        raise ValueError("Benchmark differs from input run")
    if args.show_plan:
        print(
            json.dumps(
                {
                    "benchmark": plan.benchmark,
                    "completed_cold_start": completed,
                    "counts": {s: len(plan.tasks_in(s)) for s in S.SPLITS},
                    "train_groups": {f: len(ids) for f, ids in plan.families.items()},
                },
                indent=2,
            )
        )
        return 0
    if root.exists() and any(root.iterdir()) and not args.resume:
        raise ValueError("Existing run requires --resume")
    root.mkdir(parents=True, exist_ok=True)
    lock = L.RunLock(root / "campaign.lock")
    lock.acquire()
    try:
        if args.cold_start_dir:
            cfg, plan = import_cold_start(source, root)
        # Importing a cold start restores its frozen execution config.  Apply
        # the stage's role map afterwards so Planner/Editor/Reviewer/selector
        # can intentionally differ from the L1 executor in the new run.
        cfg = apply_model_overrides(cfg, args)
        C.freeze(root / "config.json", OmegaConf.to_container(cfg, resolve=True))
        os.environ["EXPE_CONFIG_FILE"] = str(root / "config.json")
        os.environ["EXPE_TASK_FILE"] = cfg.benchmark.task_file
        if not completed:
            plan = C.ColdStart(
                cfg,
                plan,
                root,
                cold_start_workers=args.cold_start_workers,
                k=args.autonomous_attempts,
                supervised_attempts=args.supervised_attempts,
                supervised=not args.no_supervised_repair,
                family_discovery_workers=args.family_discovery_workers,
                skill_edit_mode=args.skill_edit_mode,
            ).run()
        if args.phase in ("l2", "evolve", "all"):
            config = L.EvolutionConfig(
                batch_size=args.batch_size,
                candidate_count=args.candidate_count,
                evolve_rounds=args.evolve_rounds,
                autonomous_attempts=args.autonomous_attempts,
                supervised_attempts=args.supervised_attempts,
                evolve_l1_workers=args.evolve_l1_workers,
                l2_review_workers=args.l2_review_workers,
                skill_edit_mode=args.skill_edit_mode,
                acceptance_mode=args.acceptance_mode,
                predicted_review_scope=args.predicted_review_scope,
            )
            loop = L.SerialEvolutionLoop(cfg, plan, L.LoopPaths(root), config)
            # All evolution entry points execute Skill-aware L1 before L2.
            result = loop.run_evolutions()
            print(json.dumps(result, indent=2))
        elif args.phase == "test":
            print(
                json.dumps(
                    test_evaluate(cfg, plan, root, args.test_workers), indent=2
                )
            )
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
