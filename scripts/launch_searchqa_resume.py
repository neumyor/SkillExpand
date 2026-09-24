#!/usr/bin/env python3
"""Detached, resumable SearchQA-only final evaluator."""
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(sys.argv[1]).resolve()
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 128
    if not 1 <= workers <= 256:
        raise ValueError("workers must be between 1 and 256")
    # Make this supervisor its own session leader so it survives its launcher.
    if os.getsid(0) == os.getpid():
        pass
    else:
        os.setsid()
    campaign = runpy.run_path(str(root / "code" / "run_campaign.py"))
    manifest = campaign["verify"](root)
    out = root / "full" / "searchqa" / "final-evolve1"
    log = root / "full" / "searchqa" / "logs" / "final-evolve1.log"
    status_path = root / "final-evolve1-searchqa-status.json"
    cmd = [manifest["python"], "-u", str(Path(__file__).with_name("evaluate_snapshot.py")),
           "--run-dir", str(root / "full" / "searchqa" / "run"), "--round", "1",
           "--output", str(out), "--workers", str(workers)]
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        child = subprocess.Popen(cmd, cwd=manifest["repo"],
            env=campaign["environment"](root), stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        campaign["save"](status_path, {"status": "running", "pid": child.pid,
                                        "supervisor_pid": os.getpid(), "workers": workers})
        rc = child.wait()
    campaign["save"](status_path, {"status": "complete" if rc == 0 else "needs_attention",
                                   "pid": child.pid, "supervisor_pid": os.getpid(),
                                   "workers": workers, "returncode": rc})
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
