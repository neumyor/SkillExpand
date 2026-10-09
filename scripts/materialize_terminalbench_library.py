#!/usr/bin/env python3
"""Materialize a model-generated, task-unassigned TerminalBench Skill library.

Run on an imported progressive cold-start directory.  The importer's single
bootstrap Skill is replaced exactly once by the proposed library; a directory
that already holds a model-generated library is frozen and can only be re-run
with the identical proposal.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from skillexpand import schema as S
from skillexpand.persistence.io import freeze


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--proposal", required=True, type=Path)
    args = p.parse_args()
    root, proposal_path = args.run_dir.resolve(), args.proposal.resolve()
    proposal = json.loads(proposal_path.read_text())
    rows = proposal.get("skills")
    if not isinstance(rows, list) or not rows:
        raise ValueError("proposal has no skills")
    cards = []
    for path in sorted((root / "discovery" / "results").glob("*.json"), key=lambda x: int(x.stem)):
        cards.append(json.loads(path.read_text()))
    ids = tuple(sorted(int(v["task_id"]) for v in cards))
    skills = []
    for row in rows:
        family_id = str(row["family_id"])
        skill_id = str(row["skill_id"])
        if skill_id != f"terminalbench.{family_id}":
            raise ValueError("proposal skill id/family id mismatch")
        skills.append(S.Skill(
            skill_id=skill_id, family_id=family_id, version=0,
            name=str(row["name"]), description=str(row["description"]),
            body=str(row["body"]),
            provenance=S.Provenance(
                rationale=f"Model-generated library from {len(cards)} imported schema-5 cards; no task assignment",
                source_experience_ids=tuple(v["experience_id"] for v in cards),
                source_task_ids=ids,
            ),
        ))
    payload = [S.to_dict(skill) for skill in skills]
    complete_path = root / "cold_start_complete.json"
    existing = json.loads(complete_path.read_text()) if complete_path.exists() else {}
    bootstrap = not existing.get("model_generated_library")
    complete = {
        "protocol": "progressive-library",
        "train_count": existing.get("train_count", len(cards)),
        "model_generated_library": True,
        "task_assignment": "none",
        "initial_skills_hash": S.content_hash(payload),
        "proposal_path": str(proposal_path),
    }
    if bootstrap:
        # Replace the importer's bootstrap library and marker exactly once.
        for name in ("cold_start_complete.json", "initial_skills.json"):
            if (root / name).exists():
                (root / name).unlink()
    freeze(root / "initial_skills.json", payload)
    (root / "skills.jsonl").write_text("".join(S.to_jsonl(skill) + "\n" for skill in skills))
    freeze(complete_path, complete)
    freeze(root / "library_manifest.json", {
        "protocol": "progressive-library",
        "skills": [{"skill_id": s.skill_id, "skill_key": s.key, "description": s.description}
                   for s in skills],
        "catalog_has_body": False,
        "source_cards": len(cards),
        "task_assignment": None,
    })
    print(json.dumps({"status": "materialized", "skills": len(skills), "tasks": len(cards)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
