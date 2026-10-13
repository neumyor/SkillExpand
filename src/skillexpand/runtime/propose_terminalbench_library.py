#!/usr/bin/env python3
"""Generate a model-proposed Skill library from imported TB2.1 experience cards.

This stage deliberately produces only library metadata and bodies.  It does not
assign tasks to families; routing remains a later selector decision.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

from skillexpand.l1.family_discovery import _extract_json
from skillexpand.runtime.llm_relay import relay_from_env


def _cards(root: Path) -> list[dict]:
    rows = []
    for path in sorted((root / "discovery" / "results").glob("*.json"), key=lambda p: int(p.stem)):
        value = json.loads(path.read_text())
        card = value.get("experience_card") or {}
        task = value.get("task") or card.get("task") or ""
        rows.append({
            "task_id": int(value.get("task_id", path.stem)),
            "task": str(task)[:1200],
            "reward": bool(value.get("reward", False)),
            "claims": card.get("claims", []),
            "evidence": card.get("evidence", [])[:8],
        })
    if len(rows) != 89:
        raise RuntimeError(f"expected 89 imported cards, found {len(rows)}")
    return rows


def _request(base_url: str, model: str, prompt: str, request_dir: Path, index: int) -> str:
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 12000,
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer relay"},
        method="POST",
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=180) as response:
        raw = response.read().decode()
        status = response.status
    value = json.loads(raw)
    content = value["choices"][0]["message"]["content"]
    request_dir.mkdir(parents=True, exist_ok=True)
    # Store only the request/response envelope metadata.  Prompt and response
    # are intentionally excluded from the audit directory.
    (request_dir / f"{index:03d}.meta.json").write_text(json.dumps({
        "request_index": index, "model": model, "status": status,
        "latency_seconds": round(time.time() - started, 3),
        "prompt_chars": len(prompt), "response_chars": len(content),
        "transport": "tencent_e2b_relay",
    }, indent=2) + "\n")
    return content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--model", default="qwen3.6-flash-distill")
    parser.add_argument("--llm-relay", action="store_true")
    args = parser.parse_args()
    root = args.run_dir.resolve()
    out = (args.out_dir or root / "library_proposal").resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows = _cards(root)
    compact = json.dumps(rows, ensure_ascii=False)
    prompt = """You are designing a reusable Skill library for Terminal-Bench 2.1.
Use the 89 imported experience cards below to propose the smallest defensible set of
reusable Skills. Group by required operations and completion contracts, never by task
ID, topic entity, success/failure, or answer. A proposal must be useful as a runtime
instruction body. Do not return task IDs, assignments, exclusions, or routing rules
that name tasks. Return exactly one JSON object with this schema:
{"skills":[{"skill_id":"terminalbench.family-p001","family_id":"family-p001",
"name":"...","description":"catalog-only routing description",
"trigger_conditions":["..."],"body":"numbered executable rules"}]}
Use opaque sequential family IDs family-p001, family-p002, ... and matching skill IDs.
Keep the number of skills between 2 and 8. The catalog description and triggers must
be sufficient for a selector, while body contains execution guidance only.

EXPERIENCE CARDS:
""" + compact
    relay = None
    previous = os.environ.get("EXPE_LLM_BASE_URL")
    try:
        if args.llm_relay:
            relay = relay_from_env()
            base = relay.start()
        else:
            base = os.environ.get("EXPE_LLM_BASE_URL")
            if not base:
                raise RuntimeError("relay required: pass --llm-relay")
        content = _request(base, args.model.removeprefix("openai/"), prompt, out / "usage", 0)
        value = _extract_json(content, required_keys=("skills",))
        skills = value.get("skills")
        if not isinstance(skills, list) or not 2 <= len(skills) <= 8:
            raise RuntimeError("model proposal must contain 2..8 skills")
        clean = []
        seen = set()
        for idx, row in enumerate(skills, 1):
            if not isinstance(row, dict):
                raise RuntimeError("skill proposal row is not an object")
            expected = f"family-p{idx:03d}"
            if row.get("family_id") != expected or row.get("skill_id") != f"terminalbench.{expected}":
                raise RuntimeError("skill IDs must be opaque sequential IDs")
            if expected in seen or any(k not in row for k in ("name", "description", "trigger_conditions", "body")):
                raise RuntimeError("malformed or duplicate skill proposal")
            if any(str(k).lower().endswith("task_ids") for k in row):
                raise RuntimeError("proposal contains task membership")
            seen.add(expected)
            clean.append({k: row[k] for k in ("skill_id", "family_id", "name", "description", "trigger_conditions", "body")})
        (out / "library_proposals.json").write_text(json.dumps({
            "schema_version": 1, "benchmark": "terminalbench",
            "proposal_mode": "model_generated_library_no_task_assignments",
            "source_cards": 89, "transport": "tencent_e2b_relay",
            "skills": clean,
        }, ensure_ascii=False, indent=2) + "\n")
        (out / "audit.json").write_text(json.dumps({
            "status": "passed", "catalog_has_body": False,
            "contains_task_assignments": False, "skill_count": len(clean),
            "source_cards": 89, "transport": "tencent_e2b_relay",
        }, indent=2) + "\n")
        print(json.dumps({"status": "passed", "skill_count": len(clean), "out": str(out)}))
        return 0
    finally:
        if relay is not None:
            relay.close()
        if previous is None:
            os.environ.pop("EXPE_LLM_BASE_URL", None)
        else:
            os.environ["EXPE_LLM_BASE_URL"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
