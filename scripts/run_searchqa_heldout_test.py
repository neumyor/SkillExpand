#!/usr/bin/env python3
"""Run and audit the frozen held-out SearchQA test for one condition.

The campaign wrapper normally requires both benchmarks before creating
``full/complete.json``.  ALFWorld is intentionally deferred, so this helper
uses the frozen SearchQA CLI arguments directly and records a scoped marker.
"""
import json
import os
import runpy
import subprocess
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
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_searchqa_heldout_test.py CONDITION_ROOT")
    root = Path(sys.argv[1]).resolve()
    code = root / "code" / "run_campaign.py"
    campaign = runpy.run_path(str(code))
    manifest = campaign["verify"](root)
    run = root / "full" / "searchqa" / "run"
    status = root / "full" / "searchqa" / "searchqa-test-status.json"
    save(status, {"status": "health", "pid": os.getpid(), "started": time.time()})
    campaign["health"](root)
    save(status, {"status": "evaluating", "pid": os.getpid(), "updated": time.time()})
    command = [manifest["python"], "-m", "skillexpand",
               *campaign["stage_args"](root, "full", "searchqa", "test")]
    subprocess.run(command, cwd=manifest["repo"],
                   env=campaign["environment"](root), check=True)
    audit = campaign["audit_stage"](root, "full", "searchqa", "test")
    audit_path = root / "full" / "searchqa" / "audits" / "test.json"
    save(audit_path, audit)
    record = {
        "status": "complete",
        "benchmark": "searchqa",
        "evolution_audits": [
            str(root / "full" / "searchqa" / "audits" / f"evolve-{n}.json")
            for n in (1, 2)
        ],
        "test_audit": str(audit_path),
        "finished": time.time(),
    }
    save(root / "full" / "searchqa" / "searchqa_complete.json", record)
    save(status, {"status": "complete", "pid": os.getpid(), "record": record,
                  "updated": time.time()})
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
