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
from collections import Counter

from skillexpand.l1.family_discovery import _extract_json
from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse, default_output_token_limit


def _cards(root: Path) -> list[dict]:
    source_cards = root / "source_cards.json"
    if source_cards.exists():
        rows = []
        for value in json.loads(source_cards.read_text()):
            card = value.get("experience_card") or {}
            rows.append({
                "task_id": int(value["task_id"]),
                "source_card_id": value["source_card_id"],
                "source_model": value["source_model"],
                "task": card.get("task", {}).get("text", ""),
                "reward": (None if any(a.get("reward") is None for a in value["attempts"])
                           else any(a["reward"] == 1 for a in value["attempts"])),
                "claims": card.get("claims", []),
                "evidence": card.get("evidence", [])[:8],
                "attempts": value.get("attempts", []),
            })
        if (Counter(r['source_model'] for r in rows) != {'deepseek-v4-flash-0731': 89, 'qwen3.6-flash-distill': 89}
                or len({r['source_card_id'] for r in rows}) != 178
                or len({v['experience_id'] for v in json.loads(source_cards.read_text())}) != 178):
            raise RuntimeError("expected 178 source-qualified imported cards")
        for model in {r['source_model'] for r in rows}:
            if {r['task_id'] for r in rows if r['source_model'] == model} != set(range(89)):
                raise RuntimeError('Source task coverage mismatch')
        for row in rows:
            if not row['task'] or [a['attempt_index'] for a in row['attempts']] != [1, 2, 3]:
                raise RuntimeError('Source task/attempt identity mismatch')
            for attempt in row['attempts']:
                result = json.loads(Path(attempt['result_path']).read_text())
                if (result.get('config', {}).get('agent', {}).get('model_name') != 'openai/' + row['source_model']
                        or not json.loads(Path(attempt['trajectory_path']).read_text())):
                    raise RuntimeError('Source model/trajectory mismatch')
        return rows
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


def request_payload(model, prompt):
    return {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": default_output_token_limit(model) or 12000,
        "stream": True,
        "enable_thinking": True,
    }


def cached_completion(path, expected):
    request = path.with_name(path.name.replace('.completion.json', '.request.json'))
    if not request.exists() or json.loads(request.read_text()) != expected:
        return None
    return json.loads(path.read_text())["choices"][0]["message"]["content"]


def _request(base_url: str, model: str, prompt: str, request_dir: Path, index: int, timeout: float) -> str:
    payload = request_payload(model, prompt)
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
Use the imported experience cards below to propose the smallest defensible set of
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
        accepted_request = None
        expected = request_payload(args.model.removeprefix('openai/'), prompt)
        if completions:
            try:
                cached = cached_completion(completions[-1], expected)
                if cached is None:
                    raise RuntimeError('Cached completion input mismatch')
                clean = validate_proposal(cached)
                accepted_request = str(completions[-1].with_name(completions[-1].name.replace('.completion.json', '.request.json')))
            except Exception:
                pass
        if clean is None:
            if len(list((out / 'usage').glob('*.request.json'))) >= 4:
                raise RuntimeError("Library generation format correction budget exhausted; no reset")
            if completions:
                prompt += "\n\nFORMAT CORRECTION: Return only the required JSON object, with nonempty executable bodies."
            index = len(list((out / "usage").glob("*.request.json")))
            content = _request(base, args.model.removeprefix("openai/"), prompt, out / "usage", index, args.request_timeout)
            clean = validate_proposal(content)
            accepted_request = str(out / 'usage' / f'{index:03d}.request.json')
        (out / "library_proposals.json").write_text(json.dumps({
            "schema_version": 1, "benchmark": "terminalbench",
            "proposal_mode": "model_generated_library_no_task_assignments",
            "source_cards": len(rows), "source_card_counts": {
                model: sum(r.get("source_model") == model for r in rows)
                for model in sorted({r.get("source_model") for r in rows if r.get("source_model")})
            }, "transport": "tencent_e2b_relay",
            "model": args.model, "max_tokens": default_output_token_limit(args.model) or 12000,
            "thinking": True, "stream": True,
            "skills": clean,
            "generation_request": accepted_request,
            "generation_payload": request_payload(args.model.removeprefix('openai/'), prompt),
        }, ensure_ascii=False, indent=2) + "\n")
        (out / "audit.json").write_text(json.dumps({
            "status": "passed", "catalog_has_body": False,
            "contains_task_assignments": False, "skill_count": len(clean),
            "source_cards": len(rows), "transport": "tencent_e2b_relay",
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
