#!/usr/bin/env python3
"""Repair the invalid SearchQA cold-start run in an isolated staging root.

The original run contains valid task artifacts mixed with request failures from
an interrupted process.  This tool never edits that run.  It derives the
affected task set from the persisted ledgers, runs a small smoke repair first,
then can rebuild the cold-start artifacts in a new root using the frozen
campaign code and configuration.

Typical use:

    python scripts/repair_searchqa_cold_start.py plan \
        --source-root runs/model-role-matrix-v2/baseline \
        --output-root runs/model-role-matrix-v2/baseline-searchqa-repair-v1

    python scripts/repair_searchqa_cold_start.py smoke \
        --source-root runs/model-role-matrix-v2/baseline \
        --output-root runs/model-role-matrix-v2/baseline-searchqa-repair-v1

    python scripts/repair_searchqa_cold_start.py execute \
        --source-root runs/model-role-matrix-v2/baseline \
        --output-root runs/model-role-matrix-v2/baseline-searchqa-repair-v1 \
        [--resume]

``execute`` requires a successful smoke report and uses the frozen SearchQA
worker counts.  It does not start ALFWorld or the two-benchmark supervisor.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import time


BENCHMARK = "searchqa"
MODE = "full"


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(temp, path)


def content_hash(payload):
    """Match skillexpand.schema.content_hash without importing the runtime."""
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]


def refresh_run_manifest(output_root, output_run, reason):
    """Re-sign a newly staged run against the code copied into that root.

    A run manifest is an identity record, not a cache.  Copying an old record
    after changing the frozen source tree makes the next cold-start fail before
    any task runs.  This helper only operates while constructing a new staging
    root; existing task output is never re-signed in place.
    """
    output_root, output_run = Path(output_root), Path(output_run)
    path = output_run / 'manifest.json'
    manifest = read_json(path)
    source = output_root / 'code' / 'src' / 'skillexpand'
    old_code = dict(manifest.get('code', {}))
    new_code = {
        str(p.relative_to(source)): content_hash(p.read_text())
        for p in sorted(source.rglob('*.py'))
    }
    manifest['code'] = new_code
    write_json(path, manifest)
    changed = sorted(k for k in set(old_code) | set(new_code)
                     if old_code.get(k) != new_code.get(k))
    write_json(output_run / 'repair-migration.json', {
        'status': 'complete',
        'reason': reason,
        'old_code': old_code,
        'new_code': new_code,
        'changed_code': changed,
        'created': time.time(),
    })
    return changed


@contextmanager
def probe_unlocked(path):
    """Fail before work if another process owns the source campaign lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Campaign is still running: {path}") from exc
        yield


def source_paths(source_root):
    source_root = Path(source_root).resolve()
    run = source_root / MODE / BENCHMARK / "run"
    required = [
        source_root / "manifest.json",
        source_root / "code" / "run_campaign.py",
        source_root / "inputs" / "searchqa-tasks.json",
        source_root / "inputs" / "searchqa-split.json",
        run / "manifest.json",
        run / "config.json",
        run / "split.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing source artifacts: " + ", ".join(missing))
    return source_root, run


def ledger_events(run):
    """Return task-scoped ledger events and leave shared discovery ledgers separate."""
    events = {}
    trial_dir = Path(run) / "discovery" / "trials"
    for path in sorted(trial_dir.glob("*.usage.requests.jsonl")):
        task_text = path.name.split(".", 1)[0]
        if not task_text.isdigit():
            continue
        task_id = int(task_text)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        events[task_id] = rows
    return events


def affected_tasks(run):
    affected = {}
    for task_id, rows in ledger_events(run).items():
        bad = [
            {
                "event": row.get("event"),
                "error_type": row.get("error_type"),
                "time": row.get("time"),
            }
            for row in rows
            if row.get("event") in ("error", "abandoned")
        ]
        started = {row.get("run_id") for row in rows if row.get("event") == "start"}
        terminal = {
            row.get("run_id")
            for row in rows
            if row.get("event") in ("end", "error", "abandoned")
        }
        pending = sorted(rid for rid in started - terminal if rid)
        if pending:
            bad.extend({"event": "pending", "run_id": rid} for rid in pending)
        if bad:
            affected[task_id] = bad
    return affected


def load_campaign(root):
    root = Path(root).resolve()
    code = root / "code" / "run_campaign.py"
    if not code.exists():
        raise FileNotFoundError(code)
    # Put the frozen source tree first.  The repair process must not silently
    # import the mutable checkout's skillexpand package.
    frozen_src = str(root / "code" / "src")
    if frozen_src not in sys.path:
        sys.path.insert(0, frozen_src)
    # ``environment()`` updates PYTHONPATH for child workers, but this repair
    # process has already started.  Add the frozen dependency overlay directly
    # so health checks and the parent-side cold-start imports see the same
    # runtime as spawned workers.
    manifest = read_json(root / "manifest.json")
    overlay = manifest.get("overlay")
    if overlay and str(overlay) not in sys.path:
        sys.path.insert(1, str(overlay))
    return runpy.run_path(str(code))


def prepare_shell(source_root, output_root, include_preflight, resume=False):
    """Copy only frozen campaign inputs, never mutable run outputs."""
    source_root, source_run = source_paths(source_root)
    output_root = Path(output_root).resolve()
    if output_root.exists() and any(output_root.iterdir()):
        if not resume:
            raise FileExistsError(f"Refusing to reuse non-empty output root: {output_root}")
        required = [
            output_root / "manifest.json",
            output_root / "code" / "run_campaign.py",
            output_root / "inputs" / "searchqa-tasks.json",
            output_root / MODE / BENCHMARK / "run" / "manifest.json",
            output_root / MODE / BENCHMARK / "run" / "config.json",
            output_root / MODE / BENCHMARK / "run" / "split.json",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise FileNotFoundError(
                "Existing staging root is incomplete; use a new output root: "
                + ", ".join(missing)
            )
        return output_root, output_root / MODE / BENCHMARK / "run"
    output_root.mkdir(parents=True, exist_ok=True)

    shutil.copy2(source_root / "manifest.json", output_root / "manifest.json")
    shutil.copytree(source_root / "code", output_root / "code")
    shutil.copytree(source_root / "inputs", output_root / "inputs")
    if include_preflight and (source_root / "preflight").exists():
        shutil.copytree(source_root / "preflight", output_root / "preflight")

    output_run = output_root / MODE / BENCHMARK / "run"
    output_run.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "config.json", "split.json"):
        shutil.copy2(source_run / name, output_run / name)
    # The copied config normally contains an absolute path into the source run.
    # Rewrite only the benchmark input path so workers and audits are fully
    # self-contained under the staging root.
    config_path = output_run / "config.json"
    config = read_json(config_path)
    config.setdefault("benchmark", {})["task_file"] = str(
        output_root / "inputs" / f"{BENCHMARK}-tasks.json"
    )
    write_json(config_path, config)
    # Keep the cold-start identity consistent with that relocated config and
    # the code copied into this new staging root.
    run_manifest_path = output_run / "manifest.json"
    run_manifest = read_json(run_manifest_path)
    run_manifest["config"] = config
    write_json(run_manifest_path, run_manifest)
    refresh_run_manifest(output_root, output_run,
                         reason='stage new repair run with current frozen code')
    return output_root, output_run


def copy_unaffected(source_run, output_run, affected):
    source_run = Path(source_run)
    output_run = Path(output_run)
    source_results = source_run / "discovery" / "results"
    source_trials = source_run / "discovery" / "trials"
    output_results = output_run / "discovery" / "results"
    output_trials = output_run / "discovery" / "trials"
    for task_path in sorted(source_results.glob("*.json")):
        if not task_path.stem.isdigit() or int(task_path.stem) in affected:
            continue
        task_id = task_path.stem
        trial = source_trials / f"{task_id}.json"
        usage = source_trials / f"{task_id}.usage.json"
        requests = source_trials / f"{task_id}.usage.requests.jsonl"
        required = (trial, usage, requests)
        if not all(path.exists() for path in required):
            raise FileNotFoundError(
                f"Unrepairable unaffected task {task_id}: missing checkpoint ledger"
            )
        output_results.mkdir(parents=True, exist_ok=True)
        output_trials.mkdir(parents=True, exist_ok=True)
        shutil.copy2(task_path, output_results / task_path.name)
        for path in required:
            shutil.copy2(path, output_trials / path.name)


def set_runtime(campaign, root, run):
    env = campaign["environment"](root)
    os.environ.update(env)
    os.environ["EXPE_CONFIG_FILE"] = str(Path(run) / "config.json")
    os.environ["EXPE_TASK_FILE"] = str(Path(root) / "inputs" / "searchqa-tasks.json")
    os.environ["EXPE_SPLIT_FILE"] = str(Path(run) / "split.json")


def strict_usage_audit(run):
    """Require a clean request ledger for the repaired cold-start evidence."""
    counts = {"files": 0, "start": 0, "end": 0, "error": 0, "abandoned": 0,
              "pending": 0, "missing_usage": 0}
    failures = []
    for path in sorted(Path(run).glob("**/*.requests.jsonl")):
        counts["files"] += 1
        pending = set()
        finished = set()
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            row = json.loads(line)
            event = row.get("event")
            rid = row.get("run_id")
            if event == "start":
                counts["start"] += 1
                if not rid or rid in pending or rid in finished:
                    failures.append(f"{path}:{line_no}: invalid start")
                pending.add(rid)
                continue
            if event not in ("end", "error", "abandoned") or rid not in pending:
                failures.append(f"{path}:{line_no}: unmatched terminal event")
                continue
            pending.remove(rid)
            finished.add(rid)
            counts[event] += 1
            if event == "end" and not (row.get("provider") or {}).get("token_usage"):
                counts["missing_usage"] += 1
                failures.append(f"{path}:{line_no}: missing provider token usage")
            if event in ("error", "abandoned"):
                failures.append(f"{path}:{line_no}: {event}")
        if pending:
            counts["pending"] += len(pending)
            failures.append(f"{path}: pending={len(pending)}")
    return {"status": "passed" if not failures else "failed",
            "counts": counts, "failures": failures[:100]}


def task_usage_is_clean(path):
    """Check one request ledger without treating other task ledgers as evidence."""
    path = Path(path)
    if not path.exists():
        return False
    pending = set()
    finished = set()
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
    except (OSError, json.JSONDecodeError):
        return False
    if not rows:
        return False
    for row in rows:
        event = row.get("event")
        run_id = row.get("run_id")
        if event == "start":
            if not run_id or run_id in pending or run_id in finished:
                return False
            pending.add(run_id)
        elif event in ("end", "error", "abandoned"):
            if run_id not in pending or event != "end":
                return False
            if not (row.get("provider") or {}).get("token_usage"):
                return False
            pending.remove(run_id)
            finished.add(run_id)
        else:
            return False
    return not pending


def repair_task_ready(run, task_id, S):
    """Whether a partially repaired task can safely be reused on --resume."""
    run = Path(run)
    result_path = run / "discovery" / "results" / f"{task_id}.json"
    trial_path = run / "discovery" / "trials" / f"{task_id}.json"
    request_path = run / "discovery" / "trials" / f"{task_id}.usage.requests.jsonl"
    if not all(path.exists() for path in (result_path, trial_path, request_path)):
        return False
    try:
        result = read_json(result_path)
        checkpoint = read_json(trial_path)
        experience = S.from_dict(S.TaskExperience, result)
        return (
            experience.task_id == task_id
            and experience.benchmark == BENCHMARK
            and experience.split == S.SPLIT_TRAIN
            and experience.evolution_round == 0
            and not experience.initial_skill_key
            and not experience.selected_skill_id
            and experience.experience_card is not None
            and checkpoint.get("experience") == result
            and task_usage_is_clean(request_path)
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def discard_task_artifacts(run, task_id):
    """Remove only disposable staging artifacts for one task before retrying."""
    run = Path(run)
    for directory in ("results", "errors"):
        path = run / "discovery" / directory / f"{task_id}.json"
        path.unlink(missing_ok=True)
    trials = run / "discovery" / "trials"
    for path in trials.glob(f"{task_id}.*"):
        path.unlink(missing_ok=True)


def prepare_repair_tasks(run, task_ids, S):
    """Return incomplete task IDs and clear stale failed ledgers in staging."""
    pending = []
    for task_id in sorted(task_ids):
        if not repair_task_ready(run, task_id, S):
            discard_task_artifacts(run, task_id)
            pending.append(task_id)
    return pending


def make_specs(task_ids, run, S, PL):
    run_manifest = read_json(Path(run) / "manifest.json")
    return [
        PL.ExperienceSpec(
            unit_id=f"repair:{task_id}",
            benchmark=BENCHMARK,
            task_id=task_id,
            family_id="unassigned",
            split=S.SPLIT_TRAIN,
            skill_aware=False,
            selection_source=S.SELECTION_UNSKILLED,
            max_trials=int(run_manifest["k"]),
            supervised_repair=bool(run_manifest["supervised"]),
            supervised_attempts=int(run_manifest["supervised_attempts"]),
            l1_checkpoint_path=str(Path(run) / "discovery" / "trials" / f"{task_id}.json"),
        )
        for task_id in sorted(task_ids)
    ]


def run_task_repair(root, run, task_ids, workers):
    campaign = load_campaign(root)
    set_runtime(campaign, root, run)
    from skillexpand import schema as S
    from skillexpand.runtime import parallel as PL

    results_dir = Path(run) / "discovery" / "results"
    errors_dir = Path(run) / "discovery" / "errors"
    results_dir.mkdir(parents=True, exist_ok=True)
    errors_dir.mkdir(parents=True, exist_ok=True)
    received = set()
    failures = []

    def sink(record):
        task_id = int(record["task_id"])
        if task_id not in task_ids or task_id in received:
            raise ValueError(f"Unexpected or duplicate repair result: {task_id}")
        received.add(task_id)
        if not record.get("ok"):
            failures.append(record)
            write_json(errors_dir / f"{task_id}.json", record)
            return
        experience = S.from_dict(S.TaskExperience, record["experience"])
        if experience.task_id != task_id or experience.initial_skill_key or experience.selected_skill_id:
            raise ValueError(f"Invalid repaired experience for task {task_id}")
        write_json(results_dir / f"{task_id}.json", record["experience"])

    specs = make_specs(task_ids, run, S, PL)
    PL.run_generic(specs, PL.execute_experience, workers=workers, on_result=sink)
    missing = sorted(set(task_ids) - received)
    if missing or failures:
        raise RuntimeError(f"Repair failed: missing={missing}, failed={len(failures)}")


def smoke(args):
    source_root, source_run = source_paths(args.source_root)
    affected = affected_tasks(source_run)
    if len(affected) < 2:
        raise ValueError(f"Expected at least two affected tasks, found {len(affected)}")
    api_task = next((task for task, rows in sorted(affected.items())
                     if any(row.get("error_type") in ("APIConnectionError", "KeyError")
                            for row in rows)), None)
    abandoned_task = next((task for task, rows in sorted(affected.items())
                           if any(row.get("event") == "abandoned" for row in rows)), None)
    task_ids = sorted({api_task, abandoned_task} - {None})
    if len(task_ids) < 2:
        task_ids = sorted(affected)[:2]

    smoke_root = Path(args.smoke_root or (str(Path(args.output_root).resolve()) + ".smoke"))
    smoke_root, smoke_run = prepare_shell(source_root, smoke_root, include_preflight=False)
    campaign = load_campaign(smoke_root)
    campaign["verify"](smoke_root)
    campaign["health"](smoke_root)
    run_task_repair(smoke_root, smoke_run, task_ids, workers=args.workers)
    usage = strict_usage_audit(smoke_run)
    report = {
        "status": "complete" if usage["status"] == "passed" else "needs_attention",
        "benchmark": BENCHMARK,
        "task_ids": task_ids,
        "source_root": str(source_root),
        "usage": usage,
        "finished": time.time(),
    }
    write_json(smoke_root / "smoke_complete.json", report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 1


def execute(args):
    source_root, source_run = source_paths(args.source_root)
    smoke_root = Path(args.smoke_root or (str(Path(args.output_root).resolve()) + ".smoke"))
    smoke_report = smoke_root / "smoke_complete.json"
    if not smoke_report.exists() or read_json(smoke_report).get("status") != "complete":
        raise ValueError(f"Successful smoke report required: {smoke_report}")
    affected = affected_tasks(source_run)
    if not affected:
        raise ValueError("Source run has no affected task ledgers")
    with probe_unlocked(source_run / "campaign.lock"):
        output_root, output_run = prepare_shell(
            source_root, args.output_root, include_preflight=True, resume=args.resume
        )
        copy_unaffected(source_run, output_run, set(affected))
        campaign = load_campaign(output_root)
        campaign["verify"](output_root)
        campaign["health"](output_root)
        set_runtime(campaign, output_root, output_run)
        from omegaconf import OmegaConf
        from skillexpand import schema as S
        from skillexpand.l1.cold_start import ColdStart, read_split

        pending = prepare_repair_tasks(output_run, affected, S)
        write_json(output_root / "repair_pending.json", {
            "task_ids": pending,
            "reused_task_ids": sorted(set(affected) - set(pending)),
            "time": time.time(),
        })

        cfg = OmegaConf.load(output_run / "config.json")
        plan = read_split(output_run / "split.json")
        concurrency = read_json(output_root / "manifest.json")["concurrency"][BENCHMARK]
        cold_start = ColdStart(
            cfg,
            plan,
            output_run,
            cold_start_workers=int(concurrency["cold_start_workers"]),
            k=int(read_json(output_run / "manifest.json")["k"]),
            supervised=bool(read_json(output_run / "manifest.json")["supervised"]),
            supervised_attempts=int(read_json(output_run / "manifest.json")["supervised_attempts"]),
            family_discovery_workers=int(concurrency["family_discovery_workers"]),
            card_batch_size=int(read_json(output_run / "manifest.json")["card_batch_size"]),
            skill_edit_mode=read_json(output_root / "manifest.json").get("skill_edit_mode", "rewrite"),
        )
        cold_start.run()
        audit = campaign["audit_stage"](output_root, MODE, BENCHMARK, "cold-start")
        write_json(output_root / MODE / BENCHMARK / "audits" / "cold-start.json", audit)
        usage = strict_usage_audit(output_run)
        if usage["status"] != "passed":
            raise ValueError("Strict repair usage audit failed: " + json.dumps(usage, ensure_ascii=False))
        report = {
            "status": "complete",
            "benchmark": BENCHMARK,
            "source_root": str(source_root),
            "affected_task_ids": sorted(affected),
            "repaired_task_ids": pending,
            "reused_task_ids": sorted(set(affected) - set(pending)),
            "cold_start_audit": str(output_root / MODE / BENCHMARK / "audits" / "cold-start.json"),
            "usage": usage,
            "finished": time.time(),
        }
        write_json(output_root / "repair_complete.json", report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def plan(args):
    source_root, source_run = source_paths(args.source_root)
    affected = affected_tasks(source_run)
    summary = {
        "status": "planned",
        "source_root": str(source_root),
        "output_root": str(Path(args.output_root).resolve()),
        "benchmark": BENCHMARK,
        "affected_count": len(affected),
        "affected_task_ids": sorted(affected),
        "affected_events": {str(task): rows for task, rows in sorted(affected.items())},
        "unaffected_count": sum(
            split == "train"
            for split in read_json(source_run / "split.json")["assignment"].values()
        ) - len(affected),
        "source_commit": read_json(source_root / "manifest.json").get("source_commit"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def audit(args):
    root = Path(args.output_root).resolve()
    campaign = load_campaign(root)
    campaign["verify"](root)
    audit = campaign["audit_stage"](root, MODE, BENCHMARK, "cold-start")
    run = root / MODE / BENCHMARK / "run"
    usage = strict_usage_audit(run)
    report = {"status": "complete" if usage["status"] == "passed" else "needs_attention",
              "cold_start_audit": audit, "usage": usage, "finished": time.time()}
    write_json(root / "repair_audit.json", report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "smoke", "execute", "audit"))
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--smoke-root", type=Path)
    parser.add_argument("--resume", action="store_true",
                        help="Reuse an incomplete execute staging root and repair pending tasks")
    parser.add_argument("--workers", type=int, default=1,
                        help="Workers for the two-task smoke only")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.action == "plan":
        return plan(args)
    if args.action == "smoke":
        return smoke(args)
    if args.action == "execute":
        return execute(args)
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
