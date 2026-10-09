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
from urllib.error import HTTPError
from pathlib import Path

from skillexpand.l1.family_discovery import _extract_json
from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse, default_output_token_limit


def _cards(root: Path) -> list[dict]:
    rows = []
    for path in sorted((root / "discovery" / "results").glob("*.json"), key=lambda p: int(p.stem)):
        value = json.loads(path.read_text())
        card = value.get("experience_card") or {}
        task = value.get("task") or card.get("task") or ""
        row = {
            "task_id": int(value.get("task_id", path.stem)),
            "task": str(task)[:1200],
            "reward": bool(value.get("reward", False)),
            "claims": card.get("claims", []),
            "evidence": card.get("evidence", [])[:8],
        }
        sources = value.get("raw_sources")
        if sources:
            row.pop("reward")
            row["sources"] = [{"source_model": s["source_model"],
                               "trials": s["trials"], "evidence": s["evidence"][:4]}
                              for s in sources]
            row.pop("evidence")
        rows.append(row)
    if len(rows) != 89:
        raise RuntimeError(f"expected 89 imported cards, found {len(rows)}")
    return rows


def _request(base_url: str, model: str, prompt: str, request_dir: Path, index: int, timeout: float) -> str:
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 12000,
        "stream": True,
        "enable_thinking": True,
    }).encode()
    payload = json.loads(body)
    payload["max_tokens"] = default_output_token_limit(model) or 12000
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer relay"},
        method="POST",
    )
    started = time.time()
    request_dir.mkdir(parents=True, exist_ok=True)
    (request_dir / f"{index:03d}.request.json").write_text(body.decode() + "\n")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        status = response.status
        with (request_dir / f"{index:03d}.sse").open("wb") as capture:
            def chunks():
                for line in response:
                    capture.write(line)
                    capture.flush()
                    yield line
            value = coalesce_sse(chunks())
    content = value["choices"][0]["message"]["content"]
    request_dir.mkdir(parents=True, exist_ok=True)
    (request_dir / f"{index:03d}.completion.json").write_text(json.dumps(value) + "\n")
    (request_dir / f"{index:03d}.meta.json").write_text(json.dumps({
        "request_index": index, "model": model, "status": status,
        "latency_seconds": round(time.time() - started, 3),
        "prompt_chars": len(prompt), "response_chars": len(content),
        "transport": "tencent_e2b_relay",
        "max_tokens": payload["max_tokens"], "thinking": True, "stream": True,
    }, indent=2) + "\n")
    return content


def validate_proposal(content):
    value = _extract_json(content, required_keys=("skills",))
    if set(value) != {"skills"}:
        raise RuntimeError("proposal contains unexpected top-level fields or task membership")
    skills = value.get("skills")
    if not isinstance(skills, list) or not skills:
        raise RuntimeError("model proposal must contain at least one skill")
    clean = []
    for idx, row in enumerate(skills, 1):
        expected = f"family-p{idx:03d}"
        if not isinstance(row, dict) or row.get("family_id") != expected or row.get("skill_id") != f"terminalbench.{expected}":
            raise RuntimeError("skill IDs must be opaque sequential IDs")
        if any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("name", "description", "body")):
            raise RuntimeError("empty or malformed skill fields")
        if (not isinstance(row.get("trigger_conditions"), list) or not row["trigger_conditions"] or
                any(not isinstance(t, str) or not t.strip() for t in row["trigger_conditions"])):
            raise RuntimeError("malformed trigger conditions")
        if any(str(k).lower().endswith("task_ids") or k in ("assignments", "task_assignment", "task_ids") for k in row):
            raise RuntimeError("proposal contains task membership")
        if set(row) != {"skill_id", "family_id", "name", "description", "trigger_conditions", "body"}:
            raise RuntimeError("unexpected proposal fields")
        clean.append({k: row[k] for k in ("skill_id", "family_id", "name", "description", "trigger_conditions", "body")})
    return clean


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--model", default="qwen3.6-flash-distill")
    parser.add_argument("--llm-relay", action="store_true")
    parser.add_argument("--request-timeout", type=float, default=1800.0)
    args = parser.parse_args()
    root = args.run_dir.resolve()
    out = (args.out_dir or root / "library_proposal").resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "library_proposals.json").exists():
        raise RuntimeError("Existing proposal is immutable; resume materialization instead")
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
Choose the library size from the evidence; do not target a preset number of skills.
The catalog description and triggers must be sufficient for a selector, while body
contains execution guidance only.

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
        completions = sorted((out / "usage").glob("*.completion.json"))
        clean = None
        if completions:
            cached = json.loads(completions[-1].read_text())["choices"][0]["message"]["content"]
            try:
                clean = validate_proposal(cached)
            except Exception:
                pass
        if clean is None:
            if len(completions) >= 4:
                raise RuntimeError("Library generation format correction budget exhausted; no reset")
            if completions:
                prompt += "\n\nFORMAT CORRECTION: Return only the required JSON object, with nonempty executable bodies."
            index = len(list((out / "usage").glob("*.request.json")))
            content = _request(base, args.model.removeprefix("openai/"), prompt, out / "usage", index, args.request_timeout)
            clean = validate_proposal(content)
        (out / "library_proposals.json").write_text(json.dumps({
            "schema_version": 1, "benchmark": "terminalbench",
            "proposal_mode": "model_generated_library_no_task_assignments",
            "source_cards": 89, "transport": "tencent_e2b_relay",
            "model": args.model, "max_tokens": default_output_token_limit(args.model) or 12000,
            "thinking": True, "stream": True,
            "skills": clean,
        }, ensure_ascii=False, indent=2) + "\n")
        (out / "audit.json").write_text(json.dumps({
            "status": "passed", "catalog_has_body": False,
            "contains_task_assignments": False, "skill_count": len(clean),
            "source_cards": 89, "transport": "tencent_e2b_relay",
        }, indent=2) + "\n")
        print(json.dumps({"status": "passed", "skill_count": len(clean), "out": str(out)}))
        return 0
    except Exception as exc:
        if isinstance(exc, HTTPError):
            error_class = "provider_429" if exc.code == 429 else "provider_http_error"
        elif isinstance(exc, TimeoutError):
            error_class = "provider_timeout"
        else:
            error_class = getattr(exc, "error_class", "model_proposal_error")
        message = str(exc)
        for key in ("OPENAI_API_KEY", "E2B_API_KEY"):
            if os.environ.get(key):
                message = message.replace(os.environ[key], "[REDACTED]")
        (root / "proposal_status.json").write_text(json.dumps({
            "status": "needs_attention",
            "valid": False,
            "outcome": "proposal_failure",
            "error_class": error_class,
            "transport": "tencent_e2b_relay",
            "attempts": "bounded",
            "message": message[:500],
        }, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": "needs_attention", "valid": False,
                          "error_class": error_class}))
        return 2
    finally:
        if relay is not None:
            relay.close()
        if previous is None:
            os.environ.pop("EXPE_LLM_BASE_URL", None)
        else:
            os.environ["EXPE_LLM_BASE_URL"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
