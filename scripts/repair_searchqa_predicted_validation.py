#!/usr/bin/env python3
"""Retry missing predicted-validation records for a frozen SearchQA run."""
import json
import os
import runpy
import sys
import time
from pathlib import Path


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: repair_searchqa_predicted_validation.py CONDITION_ROOT [SKILL_ID]")
    root = Path(sys.argv[1]).resolve()
    only_skill = sys.argv[2] if len(sys.argv) == 3 else None
    campaign = runpy.run_path(str(root / "code" / "run_campaign.py"))
    manifest = campaign["verify"](root)
    env = campaign["environment"](root)
    os.environ.update(env)
    run = root / "full" / "searchqa" / "run"
    os.environ["EXPE_CONFIG_FILE"] = str(run / "config.json")
    os.environ["EXPE_TASK_FILE"] = str(json.loads((run / "config.json").read_text())[
        "benchmark"]["task_file"])
    for path in reversed(env["PYTHONPATH"].split(os.pathsep)):
        if path not in sys.path:
            sys.path.insert(0, path)

    from omegaconf import OmegaConf
    from skillexpand import schema as S
    from skillexpand.evaluation.routing import FrozenRoutes
    from skillexpand.evaluation.validation import PredictedSkillScorer, ScoreCache
    from skillexpand.persistence.artifacts import load_cold_start
    from skillexpand.runtime import agent_factory as F

    cfg, plan, initial, _ = load_cold_start(run)
    routes = FrozenRoutes.load_existing(cfg, plan, initial, run / "routes", S.SPLIT_VAL)
    cache_path = run / "val" / "predicted_scores.jsonl"
    # Repair every initial Skill panel with a missing task. The failed attempt
    # was on the v0 base panel; handling all panels keeps this helper resumable
    # if a subsequent candidate call encounters the same transient issue.
    skills_to_repair = list(initial)
    skipped = []
    for candidate_path in sorted((run / "l2_proposals").glob("*/candidate-*.json")):
        try:
            payload = json.loads(candidate_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append({"path": str(candidate_path), "reason": f"invalid_json: {exc}"})
            continue
        if not isinstance(payload, dict):
            skipped.append({"path": str(candidate_path), "reason": "top_level_not_object"})
            continue
        candidate = payload.get("candidate")
        if not isinstance(candidate, dict):
            skipped.append({"path": str(candidate_path), "reason": "candidate_not_object"})
            continue
        raw_skill = candidate.get("skill")
        if not isinstance(raw_skill, dict):
            skipped.append({"path": str(candidate_path), "reason": "skill_not_object"})
            continue
        try:
            skills_to_repair.append(S.from_dict(S.Skill, raw_skill))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            skipped.append({"path": str(candidate_path), "reason": f"invalid_skill: {exc}"})
    def judge_factory(task_id, skill, usage_path):
        role_cfg = OmegaConf.create(OmegaConf.to_container(cfg, resolve=True))
        role_cfg.agent.llm = F.role_model(cfg, "l2_reviewer")
        role_cfg.models = OmegaConf.create({
            **dict(OmegaConf.to_container(cfg.get("models", {}), resolve=True)),
            "l2_reviewer": role_cfg.agent.llm,
        })
        return F.build_reasoning_host(role_cfg, str(usage_path), role="l2_reviewer")

    scorer = PredictedSkillScorer(cfg, routes, ScoreCache(cache_path),
                                  manifest["concurrency"]["searchqa"]["l2_review_workers"],
                                  judge_factory=judge_factory)

    skill_refs = {}
    for skill in skills_to_repair:
        if only_skill and skill.skill_id != only_skill:
            continue
        ref = skill.key + "#" + S.content_hash(skill.body)
        skill_refs[ref] = skill
    missing = {}
    for ref, skill in skill_refs.items():
        panel = f"val:{routes.fingerprint}:{skill.skill_id}"
        todo = []
        for task_id in routes.groups[skill.skill_id]:
            key = ScoreCache.make_key(cfg.benchmark.name, panel, task_id,
                                      f"predicted:{scorer.protocol_hash}", skill.body)
            if scorer.cache.get(key) is None:
                todo.append(task_id)
        if todo:
            missing[ref] = todo
    if not missing:
        report = {"status": "complete", "missing_before": {}, "skipped": skipped,
                  "finished": time.time()}
        save(run / "predicted-validation-repair.json", report)
        print(json.dumps(report))
        return 0
    repaired = {}
    repairs = []
    failures = []
    for skill_key, task_ids in missing.items():
        skill = skill_refs[skill_key]
        panel = f"val:{routes.fingerprint}:{skill.skill_id}"
        keys = {
            task_id: ScoreCache.make_key(cfg.benchmark.name, panel, task_id,
                                         f"predicted:{scorer.protocol_hash}", skill.body)
            for task_id in task_ids
        }
        for task_id in task_ids:
            body_hash = S.content_hash(skill.body)
            usage_path = run / "val" / "usage" / f"repair-predicted-{skill.skill_id}-{body_hash}-{task_id}.json"
            host = judge_factory(task_id, skill, usage_path)
            prompt = scorer.prompt(F.task_text_of(cfg, task_id), skill)
            format_attempts = 0
            try:
                parsed, format_attempts = scorer._review(host, prompt)
            except Exception as exc:
                failures.append({"skill_key": skill_key, "task_id": task_id,
                                 "error": f"{type(exc).__name__}: {exc}"})
                continue
            if parsed.get("reason_truncated"):
                repairs.append({"skill_key": skill_key, "task_id": task_id,
                                "normalization": "truncate_reason_to_512"})
            record = {
                "task_id": task_id, "skill_key": skill.key, "cache_key": keys[task_id],
                "panel_key": panel, "protocol_hash": scorer.protocol_hash,
                "format_attempts": format_attempts, "response_format": "json_schema",
                **parsed,
            }
            scorer.cache.put(keys[task_id], record)
        repaired[skill_key] = task_ids
    report = {"status": "complete" if not failures else "partial", "missing_before": missing,
              "repaired": repaired, "format_repairs": repairs, "failures": failures,
              "skipped": skipped, "finished": time.time()}
    save(run / "predicted-validation-repair.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not failures else 1

if __name__ == "__main__":
    main()
