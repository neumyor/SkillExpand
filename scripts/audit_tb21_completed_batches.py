"""Audit completed closed-set predicted batches without making LLM requests."""

import argparse
from collections import Counter
import json
from pathlib import Path


def audit(run):
    load = lambda name: json.loads((run / name).read_text())
    result, original_audit = load("result.json"), load("audit.json")
    expected = set(range(89))
    cache = [json.loads(line) for line in (run / "train/predicted_scores.jsonl").read_text().splitlines()]
    assert len(cache) == len({row["cache_key"] for row in cache}) == 534
    assert Counter(row["task_id"] for row in cache) == Counter({task: 6 for task in expected})
    ledger = load("continuation_ledger.json")
    assert ledger["diagnostic_scores_imported"] == 0
    journals = []
    seen = set()
    rows = []
    approved = 0
    for path in sorted((run / "l2_batches").glob("*.json")):
        journal = json.loads(path.read_text())
        tasks = journal["task_ids"]
        assert len(tasks) == len(set(tasks)) and not seen.intersection(tasks)
        seen.update(tasks)
        assert not journal["review_errors"]
        acceptance = journal["acceptance"]
        assert set(acceptance["task_ids"]) == expected
        passed = []
        for candidate in acceptance["candidates"]:
            panel = candidate["result"]
            assert len(panel["task_ids"]) == 89 and set(panel["task_ids"]) == expected
            assert len(panel["arms"]) == 2
            for arm in panel["arms"]:
                ids = [item["task_id"] for item in arm["outcomes"]]
                assert len(ids) == 89 and set(ids) == expected
            metrics = panel["metrics"]
            assert metrics["n_paired"] == 89
            improves = metrics["mean_candidate"] > metrics["mean_base"]
            assert panel["passed"] == improves
            if improves:
                passed.append(candidate["candidate_id"])
            rows.append({"batch": path.stem, "candidate": candidate["candidate_id"],
                         "base": metrics["mean_base"], "candidate_mean": metrics["mean_candidate"],
                         "passed": improves})
        selected = journal["selected_candidate_id"]
        if journal["outcome"] == "review_approved":
            assert selected in passed
            approved += 1
        else:
            assert journal["outcome"] == "hold" and selected is None and not passed
        journals.append({"path": str(path.resolve()), "cards": len(tasks),
                         "outcome": journal["outcome"], "selected_candidate_id": selected})
    assert seen == expected and len(journals) == 2
    assert result["completed_batches"] == 2 and result["invalid_batches"] == 0
    assert result["review_approved_updates"] == original_audit["review_approved"] == approved
    assert result["acceptance_mode"] == "predicted" and not result["empirically_validated"]
    report = {"status": "passed", "run": str(run.resolve()), "tasks": 89,
              "cache_records": len(cache), "unique_cache_keys": len(cache),
              "cache_entries_per_task": 6, "input_gate_audit": ledger["input_gate_audit"],
              "batches": journals, "review_approved_updates": approved,
              "final_skills": result["skills"], "candidates": rows,
              "interpretation": "Closed-set predicted acceptance; not verifier pass rates or independent test generalization."}
    (run / "completion_audit.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Closed-Set Evolution Completion", "", f"Run: `{run.resolve()}`", "",
             f"Audit passed: 89 unique cards in 2 disjoint batches; {approved} approved updates.",
             "Each candidate has two complete 89-task arms. Strict candidate_mean > base_mean checked.",
             "These are predicted reviewer means, not verifier pass@1/pass@3 or independent test results.", "",
             "| Batch | Candidate | Base Mean | Candidate Mean | Passed |",
             "| --- | --- | ---: | ---: | --- |"]
    for row in rows:
        lines.append(f"| {row['batch']} | {row['candidate']} | {row['base']:.6f} | {row['candidate_mean']:.6f} | {row['passed']} |")
    lines.extend(["", "Final skills: `" + json.dumps(result["skills"]) + "`", "",
                  "Evidence: completion_audit.json, result.json, audit.json, l2_batches/*.json."])
    (run / "completion_report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    for directory in parser.parse_args().runs:
        audit(directory)
