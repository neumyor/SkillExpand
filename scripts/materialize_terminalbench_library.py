#!/usr/bin/env python3
"""Materialize a model-generated, task-unassigned TB2.1 Skill library."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from skillexpand import schema as S
from skillexpand.l1.cold_start import freeze


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
        value = json.loads(path.read_text())
        cards.append(value)
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
                rationale="Model-generated library from 89 imported schema-5 cards; no task assignment",
                source_experience_ids=tuple(v["experience_id"] for v in cards),
                source_task_ids=ids,
            ),
        ))
    payload = [S.to_dict(skill) for skill in skills]
    freeze(root / "initial_skills.json", payload)
    (root / "skills.jsonl").write_text("".join(S.to_jsonl(skill) + "\n" for skill in skills))
    complete_path = root / "cold_start_complete.json"
    complete = (json.loads(complete_path.read_text()) if complete_path.exists() else {
        "protocol": "progressive-library", "train_count": len(cards),
    })
    complete.update({
        "protocol": "progressive-library",
        "model_generated_library": True,
        "task_assignment": "none",
        "initial_skills_hash": S.content_hash(payload),
        "proposal_path": str(proposal_path),
    })
    # A fresh formal run starts from a copied imported directory.  Replace its
    # bootstrap completion marker exactly once with the model-generated library
    # marker; subsequent runs should use the existing materialized directory.
    if complete_path.exists():
        complete_path.unlink()
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
