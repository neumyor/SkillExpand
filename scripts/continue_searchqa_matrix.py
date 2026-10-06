#!/usr/bin/env python3
"""Resume the five-condition SearchQA evolution and final test sequentially.

The repaired baseline has a separate root because its original cold-start
ledger was incomplete.  ALFWorld is deliberately excluded from this runner.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MATRIX = REPO / "runs" / "model-role-matrix-v2"
CONDITIONS = (
    ("baseline", MATRIX / "baseline-searchqa-repair-v1"),
    ("l1_executor-strong", MATRIX / "l1_executor-strong"),
    ("cold_start-strong", MATRIX / "cold_start-strong"),
    ("l2_planner-strong", MATRIX / "l2_planner-strong"),
    ("l2_reviewer-strong", MATRIX / "l2_reviewer-strong"),
)


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def pid_command(pid):
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                text=True, capture_output=True, check=False)
    except OSError:
        return ""
    return result.stdout.strip()


def live_for_root(pid, root):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    command = pid_command(pid)
    # The repaired supervisor embeds the repository path in a Python variable
    # and therefore does not expose the full root path in its command line.
    # The unique condition directory name plus the runner name is sufficient
    # here, while the child PID remains the authoritative liveness handle.
    return (str(root) in command or root.name in command) and (
        "run_campaign.py" in command or "run_searchqa_heldout_test.py" in command)


def campaign_command(root):
    manifest = read(root / "manifest.json")
    return [manifest["python"], "-u", str(root / "code" / "run_campaign.py"),
            "_job", "--root", str(root), "--mode", "full", "--benchmark", "searchqa"]


def process_env(root):
    """Build the startup environment used by frozen campaign children."""
    manifest = read(root / "manifest.json")
    env = os.environ.copy()
    models = manifest.get("models") or {}
    default_model = manifest.get("model")
    l1_model = models.get("l1_executor") or default_model
    env.update(
        EXPE_LLM_MODEL=l1_model,
        EXPE_LLM_DISABLE_THINKING="1",
        EXPE_LLM_ENABLE_THINKING_MODELS=",".join(sorted({
            str(value) for value in models.values() if str(value).startswith("glm-5.3")
        })),
        EXPE_SHOW_ADMISSIBLE="1",
        PYTHONPATH=str(root / "code" / "src") + os.pathsep + manifest["overlay"],
        ALFWORLD_DATA=manifest["alfworld_data"],
        ALFWORLD_CONFIG=manifest["alfworld_config"],
        ALFWORLD_BENCH_SRC=manifest["alfworld_bench_src"],
        EXPE_LLM_TIMEOUT_SECONDS=str(manifest["timeouts"]["request"]),
        EXPE_LLM_RETRIES=str(manifest["timeouts"]["request_retries"]),
        EXPE_LLM_GATE_FILE=str(root / "request-gate.state"),
        EXPE_LLM_REQUEST_INTERVAL_SECONDS=str(manifest["request_interval_seconds"]),
        EXPE_ENV_TIMEOUT_SECONDS=str(manifest["timeouts"]["environment"]),
        EXPE_WORKER_TIMEOUT_SECONDS=str(manifest["timeouts"]["worker_progress"]),
        PYTHONUNBUFFERED="1",
    )
    return env


def wait_existing(root, status, state):
    pid = state.get("pid")
    while state.get("status") in ("running", "retry_wait") and live_for_root(pid, root):
        time.sleep(30)
        if status.exists():
            state = read(status)
            pid = state.get("pid") or pid
    return state


def run_evolution(root, state):
    status = root / "full" / "searchqa" / "status.json"
    if status.exists():
        state = wait_existing(root, status, state)
    if state.get("status") == "complete":
        return state
    # A stale status is resumable; run_campaign's job lock and per-stage resume
    # state determine the next attempt.  The child is tracked directly.
    manifest = read(root / "manifest.json")
    log_path = root / "full" / "searchqa" / "searchqa-resume.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        child = subprocess.Popen(campaign_command(root), cwd=manifest["repo"],
                                 env=process_env(root), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        save(root / "full" / "searchqa" / "searchqa-resume.pid.json",
             {"pid": child.pid, "started": time.time()})
        returncode = child.wait()
    if status.exists():
        state = read(status)
    state["launcher_returncode"] = returncode
    if returncode != 0 or state.get("status") != "complete":
        raise RuntimeError(f"SearchQA evolution failed for {root.name}: {state}")
    return state


def run_test(root):
    marker = root / "full" / "searchqa" / "searchqa_complete.json"
    if marker.exists():
        return read(marker)
    manifest = read(root / "manifest.json")
    log_path = root / "full" / "searchqa" / "searchqa-test.log"
    with log_path.open("ab") as log:
        child = subprocess.Popen(
            [manifest["python"], "-u", str(REPO / "scripts" / "run_searchqa_heldout_test.py"), str(root)],
            cwd=manifest["repo"], env=process_env(root), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        save(root / "full" / "searchqa" / "searchqa-test.pid.json",
             {"pid": child.pid, "started": time.time()})
        returncode = child.wait()
    if returncode != 0 or not marker.exists():
        raise RuntimeError(f"SearchQA held-out test failed for {root.name}; returncode={returncode}")
    return read(marker)


def run_snapshots(root):
    marker = root / "full" / "searchqa" / "run" / "test-snapshots-complete.json"
    if marker.exists():
        return read(marker)
    manifest = read(root / "manifest.json")
    log_path = root / "full" / "searchqa" / "searchqa-snapshots.log"
    with log_path.open("ab") as log:
        child = subprocess.Popen(
            [manifest["python"], "-u", str(REPO / "scripts" / "run_searchqa_snapshots.py"), str(root)],
            cwd=manifest["repo"], env=process_env(root), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        save(root / "full" / "searchqa" / "searchqa-snapshots.pid.json",
             {"pid": child.pid, "started": time.time()})
        returncode = child.wait()
    if returncode != 0 or not marker.exists():
        raise RuntimeError(f"SearchQA snapshot evaluation failed for {root.name}; returncode={returncode}")
    return read(marker)


def main():
    if os.getsid(0) != os.getpid():
        os.setsid()
    status_path = MATRIX / "searchqa-final-status.json"
    state = {"status": "running", "benchmark": "searchqa", "started": time.time(),
             "conditions": [name for name, _ in CONDITIONS], "results": {}}
    save(status_path, state)
    try:
        for name, root in CONDITIONS:
            state["current"] = name
            state["results"][name] = {"status": "evolving", "root": str(root)}
            save(status_path, state)
            if not root.exists():
                raise FileNotFoundError(root)
            # Frozen check and real endpoint health are done in a fresh process
            # for each condition, avoiding imported-code cross-contamination.
            manifest = read(root / "manifest.json")
            subprocess.run([manifest["python"], "-u", str(root / "code" / "run_campaign.py"),
                            "check", "--root", str(root)], cwd=manifest["repo"],
                           env=process_env(root), check=True)
            subprocess.run([manifest["python"], "-u", str(root / "code" / "run_campaign.py"),
                            "health", "--root", str(root)], cwd=manifest["repo"],
                           env=process_env(root), check=True)
            state0 = read(root / "full" / "searchqa" / "status.json") if (
                root / "full" / "searchqa" / "status.json").exists() else {}
            evolution = run_evolution(root, state0)
            state["results"][name]["evolution"] = evolution
            state["results"][name]["status"] = "testing"
            save(status_path, state)
            result = run_test(root)
            state["results"][name]["test"] = result
            state["results"][name]["status"] = "snapshots"
            save(status_path, state)
            state["results"][name]["snapshots"] = run_snapshots(root)
            state["results"][name]["status"] = "complete"
            save(status_path, state)
        state.update(status="searchqa_complete", current=None, finished=time.time(),
                     deferred_benchmarks=["alfworld"])
        save(status_path, state)
        return 0
    except BaseException as exc:
        state.update(status="needs_attention", error=f"{type(exc).__name__}: {exc}",
                     current=state.get("current"), finished=time.time())
        save(status_path, state)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
