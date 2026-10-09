"""Fresh E1-style E3/E5 cold start, independent Harbor slots, and family evolution."""
import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.l1 import learning as L, protocol as P
from skillexpand.l1.cold_start import freeze
from skillexpand.l2.loop import EvolutionConfig, LoopPaths, RunLock, SerialEvolutionLoop
from skillexpand.persistence.artifacts import load_cold_start, provider_signature
from skillexpand.runtime.llm_relay import relay_from_env
from skillexpand.runtime.progressive import select_and_load
from skillexpand.benchmarks.terminalbench import harbor_rollout
from run_tb21_empirical import CANARIES, SelectorHost, active_worker_reservation, failure_class
from summarize_tb21_empirical import read, write, validate_trial

METHOD = "DEEPSEEK_up5zdj"
QWEN = "qwen3.6-flash-distill"


def source_rows(source, tasks, model, accepted_qwen=False):
    grouped = {t["task_name"]: [] for t in tasks}
    for path in sorted(source.glob("*/result.json")):
        result = read(path)
        if result.get("task_name") in grouped:
            grouped[result["task_name"]].append((path, result))
    rows = []
    for task, entry in enumerate(tasks):
        trials = grouped[entry["task_name"]]
        if len(trials) != 3:
            raise ValueError(f"Raw coverage mismatch: {entry['task_name']} has {len(trials)} attempts")
        for attempt, (path, result) in enumerate(trials, 1):
            actual = result.get("config", {}).get("agent", {}).get("model_name")
            if actual != "openai/" + model:
                raise ValueError(f"Raw source model mismatch: {path}")
            exception = result.get("exception_info") or {}
            category = exception.get("exception_type", "")
            if exception and "timeout" not in category.lower() and not accepted_qwen:
                raise ValueError(f"Unaccepted raw exception: {path}: {category}")
            reward = (result.get("verifier_result") or {}).get("rewards", {}).get("reward")
            if reward not in (0, 1) or isinstance(reward, bool):
                reward = None
            if reward is None and not exception:
                raise ValueError(f"Missing raw verifier without accepted exception: {path}")
            trajectory = path.parent / "agent/trajectory.json"
            if not trajectory.is_file():
                raise ValueError(f"Raw trajectory missing: {trajectory}")
            rows.append({"source_model": model, "task_id": task, "task_name": entry["task_name"],
                         "attempt_index": attempt, "result_path": str(path.resolve()),
                         "trajectory_path": str(trajectory.resolve()), "reward": reward,
                         "exception_type": category or None,
                         "input_policy": "user_accepted_qwen" if accepted_qwen else "timeout_accepted"})
    return rows


def raw_trial(row, index):
    steps = read(row["trajectory_path"]).get("steps", [])
    events = [{"ref": f"e{i+1}", "action": "TerminalBatch",
               "observation": str(step.get("message") or step.get("observation") or ""),
               "environment": {"success": False}} for i, step in enumerate(steps)
              if step.get("message") or step.get("observation")]
    return {"index": index, "phase": "autonomous", "status": "completed",
            "success": row["reward"] == 1, "verifier_reward": row["reward"],
            "termination": row.get("exception_type") or "verifier",
            "source_model": row["source_model"], "source_attempt_index": row["attempt_index"],
            "trajectory": row["trajectory_path"], "result_path": row["result_path"], "events": events}


def experience(task, instruction, trials, family="terminalbench.general", skill=None, selection=None):
    evidence = P.evidence(instruction, trials)
    card = L.card(task, instruction, trials, {"status": "valid", "claims": []},
                  "external_harbor", "tb21-aligned", len, evidence=evidence,
                  card_id=f"{'evolution:1' if skill else 'discovery'}:{task}:card",
                  benchmark="terminalbench", family_id=family,
                  evolution_round=1 if skill else 0, skill_key=skill.key if skill else None)
    rewards = tuple(t["success"] for t in trials)
    selection = selection or {}
    return S.TaskExperience(
        f"{'evolution:1' if skill else 'discovery'}:{task}:experience", "terminalbench", task,
        instruction, family, S.SPLIT_TRAIN, any(rewards), len(trials),
        initial_skill_key=skill.key if skill else None,
        failed_trajectories=tuple(t["trajectory"] for t in trials if not t["success"]),
        final_trajectory=next((t["trajectory"] for t in reversed(trials) if t["success"]), None),
        selected_skill_id=skill.skill_id if skill else None,
        selection_source=S.SELECTION_AGENT if skill else S.SELECTION_UNSKILLED,
        selection_reason=selection.get("why", ""), selection_raw=selection.get("raw", ""),
        selection_catalog=tuple(selection.get("catalog", [])),
        skill_load={"load_stage": "after_selection", "skill_key": skill.key,
                    "skill_id": skill.skill_id} if skill else {},
        trial_rewards=rewards, trial_phases=("autonomous",) * len(trials),
        experience_card=card, l1_trials=tuple(trials), evolution_round=1 if skill else 0)


def source_payloads(task, instruction, rows):
    sources = []
    for model in sorted({r["source_model"] for r in rows}):
        selected = [r for r in rows if r["source_model"] == model and r["task_id"] == task]
        if len(selected) != 3:
            raise ValueError("Missing source/task attempts")
        trials = [raw_trial(r, i) for i, r in enumerate(selected, 1)]
        evidence = experience(task, instruction, trials).experience_card["evidence"]
        sources.append({"source_model": model, "trials": selected, "evidence": evidence[:4]})
    # Balance evidence by the shorter source; do not substitute the other source.
    limit = min(len(s["evidence"]) for s in sources)
    for source in sources:
        source["evidence"] = source["evidence"][:limit]
    return sources


def prepare(args):
    root = args.run_dir.resolve()
    identity = {"stage": args.stage, "protocol": "tb21-e1-aligned-free-library-v1",
                "sources": [str(args.deepseek_source.resolve())] +
                           ([str(args.qwen_source.resolve())] if args.stage == "E5" else []),
                "task_file": str(args.task_file.resolve()), "workers": args.workers,
                "executor_model": METHOD if args.stage == "E3" else QWEN,
                "method_model": METHOD, "scope": "89-task closed-set", "persistence": 0,
                "old_bootstrap_results_imported": False, "correction_budget_resets": 0}
    if root.exists():
        if read(root / "alignment.json") != identity:
            raise ValueError("Existing run identity differs; old runs cannot be imported")
        if (root / "input_audit.json").exists() and read(root / "input_audit.json").get("passed"):
            return root
    tasks = read(args.task_file)
    if len(tasks) != 89 or len({t["task_name"] for t in tasks}) != 89:
        raise ValueError("Expected 89 unique tasks")
    rows = source_rows(args.deepseek_source, tasks, "deepseek-v4-flash-0731")
    if args.stage == "E5":
        gate = read(args.raw_gate)
        if not gate["input_gate_passed"]["E5"]:
            raise ValueError("Qwen accepted input gate is not passed")
        rows += source_rows(args.qwen_source, tasks, QWEN, accepted_qwen=True)
    keys = [(r["source_model"], r["task_id"], r["attempt_index"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate raw identity")
    root.mkdir(parents=True, exist_ok=True)
    write(root / "alignment.json", identity)
    write(root / "tasks.json", tasks)
    write(root / "raw_inputs.json", rows)
    split = S.SplitPlan.make({i: S.SPLIT_TRAIN for i in range(89)}, "terminalbench", 42)
    cfg = {"agent": {"llm": identity["executor_model"]},
           "benchmark": {"name": "terminalbench", "task_prefix": "", "task_file": identity["task_file"],
                         "max_steps": 1, "num_fewshots": 0, "ai_name": "terminal agent",
                         "env": {"show_admissible_commands": False},
                         "l1": {"adapter": "skillexpand.l1.adapters:TerminalBenchAdapter"},
                         "progressive_library": True, "rollout": {
                             "mode": "harbor_rollout", "llm_transport": "tencent_e2b_relay",
                             "runner_script": "/data2/liyishan/tb21-tencent-skill/scripts/run_tencent_tb21_smoke.sh",
                             "provider_base_url": "https://llm-center.modelbest.co/v1",
                             "direct_provider_fallback": False}},
           "models": {role: METHOD for role in ("cold_start", "selector", "l2_planner", "l2_editor", "l2_reviewer")}}
    cfg["models"]["l1_executor"] = identity["executor_model"]
    write(root / "config.json", cfg)
    write(root / "split.json", S.to_dict(split))
    table = [{"task": t["instruction"], "env_kwargs": {"instruction": t["instruction"], "task_name": t["task_name"]},
              "env_name": "terminalbench"} for t in tasks]
    write(root / "manifest.json", {**identity, "protocol": "progressive-library-experience-first",
          "split": S.to_dict(split), "config": cfg, "task_table_hash": S.content_hash(table),
          "prompts": {}, "k": 3, "supervised": False, "supervised_attempts": 0,
          "card_batch_size": 12, "skill_edit_mode": "rewrite", "routing_mode": "progressive_library",
          "acceptance_panel": "all_train", "trajectory_import": {"records": len(rows),
          "sources": Counter(r["source_model"] for r in rows), "ledger": str(root / "raw_inputs.json")}})
    hashes = {}
    for task, entry in enumerate(tasks):
        own = [r for r in rows if r["task_id"] == task]
        trials = [raw_trial(r, i) for i, r in enumerate(own, 1)]
        exp = experience(task, entry["instruction"], trials)
        payload = S.to_dict(exp)
        payload["raw_sources"] = source_payloads(task, entry["instruction"], rows)
        freeze(root / f"discovery/results/{task}.json", payload)
        hashes[str(task)] = S.content_hash(P.projection(exp.experience_card))
    freeze(root / "discovery/card_hashes.json", hashes)
    write(root / "input_audit.json", {"passed": True, "tasks": 89, "records": len(rows),
          "unique_source_task_attempt_keys": len(keys), "source_counts": Counter(r["source_model"] for r in rows),
          "exceptions": dict(Counter(r["exception_type"] for r in rows if r["exception_type"])),
          "unknown_rewards_preserved": sum(r["reward"] is None for r in rows),
          "verifier_baseline_complete": all(r["reward"] is not None and not r["exception_type"] for r in rows)})
    write(root / "status.json", {"stage": args.stage, "status": "prepared", "workers": args.workers})
    return root


def run_slot(root, cfg, skills, tasks, transport, task, attempt, model):
    path = root / f"slots/{task:02d}-{attempt}/record.json"
    if path.exists() and read(path).get("status") == "valid":
        record = read(path)
        validate_trial(record["result_path"], tasks[task]["task_name"], activation=True, model=model)
        return record
    request_root = path.parent / "requests"
    request_root.mkdir(parents=True, exist_ok=True)
    for retry in range(3):
        directory = request_root / f"{len(list(request_root.iterdir()))+1:03d}"
        directory.mkdir()
        record = {"task_id": task, "attempt_index": attempt, "task_name": tasks[task]["task_name"]}
        try:
            skill, selection = select_and_load(SelectorHost(transport, directory / "selector.json", METHOD),
                                               tasks[task]["instruction"], skills)
            write(directory / "selection.json", selection)
            name = "install-windows-3-11" if record["task_name"] == "install-windows-3.11" else record["task_name"]
            mounted_source = directory / "skills" / name / "SKILL.md"
            mounted_source.parent.mkdir(parents=True, exist_ok=True)
            mounted_source.write_text(skill.body)
            rollout, runtime = harbor_rollout(cfg, task, skill, 1, directory, evolution_round=1)
            write(directory / "rollout.json", {"rollout": rollout, "runtime": runtime})
            if len(rollout["trials"]) != 1:
                raise ValueError("Missing or duplicate Harbor trial")
            trial = rollout["trials"][0]
            _, reward, trajectory = validate_trial(trial["result_path"], record["task_name"], activation=True, model=model)
            activation = read(Path(trial["result_path"]).parent / "agent/skill_activation.json")
            mounted = Path(activation["source_path"])
            if not mounted.is_relative_to(directory) or mounted.read_text() != skill.body:
                raise ValueError("Skill activation body/path mismatch")
            record.update(status="valid", reward=reward, result_path=trial["result_path"],
                          trajectory_path=trajectory, selection=selection, skill_file=str(mounted))
            write(path, record)
            return record
        except Exception as exc:
            message = str(exc)
            for key in ("OPENAI_API_KEY", "E2B_API_KEY", "TB21_METHOD_API_KEY", "TB21_EXECUTOR_API_KEY"):
                if os.environ.get(key):
                    message = message.replace(os.environ[key], "[REDACTED]")
            record.update(status="infrastructure_error", error_class=failure_class(exc), error=message[:1500])
            write(directory / "error.json", record)
            write(path, record)
    return record


def collect_cards(root, tasks, skills, model):
    rows = [read(root / f"slots/{task:02d}-{a}/record.json") for task in range(89) for a in (1, 2, 3)]
    if any(r["status"] != "valid" for r in rows):
        raise ValueError("Incomplete valid slot coverage; L2 remains locked")
    for task, entry in enumerate(tasks):
        own = [r for r in rows if r["task_id"] == task]
        ids = {r["selection"]["loaded_skill_key"] for r in own}
        if len(ids) != 1:
            raise ValueError(f"Independent selectors disagree for task {task}; cannot assign one family card")
        skill = next(s for s in skills if s.key in ids)
        trials = []
        for row in own:
            validate_trial(row["result_path"], entry["task_name"], activation=True, model=model)
            trial = raw_trial({**row, "source_model": model}, row["attempt_index"])
            trial["selection"] = row["selection"]
            trials.append(trial)
        exp = experience(task, entry["instruction"], trials, skill.family_id, skill, own[0]["selection"])
        freeze(root / f"evolution/round-1/cards/{task}.json", S.to_dict(exp))
    write(root / "execution_audit.json", {"passed": True, "valid_slots": len(rows),
          "unique_keys": len({(r["task_id"], r["attempt_index"]) for r in rows}),
          "executor": model, "independent_selection_per_attempt": True,
          "initial_library_execution": True, "not_final_library_empirical": True})


def reserve(root, stage, workers):
    # Serialize check + publication so simultaneous E3/E5 launches cannot overbook.
    with (root.parent / ".tb21-worker-reservations.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reserved, active = active_worker_reservation(root.parent, root)
        if workers + reserved > 100:
            raise ValueError(f"Worker cap exceeded: {reserved}+{workers}")
        write(root / "capacity.json", {"workers": workers, "other_reserved": reserved, "active": active})
        write(root / "status.json", {"stage": stage, "status": "running", "pid": os.getpid(),
                                    "workers": workers, "phase": "cold_start"})


def install_review_budget(loop, root, tasks):
    scorer = loop._ensure_predicted_scorer()
    task_ids = {entry["instruction"]: i for i, entry in enumerate(tasks)}

    def review(host, prompt):
        payload = json.loads(prompt)
        task = task_ids[payload["task"]]
        body = S.content_hash(payload["skill"]["body"])
        path = root / "review_budgets" / f"{task}-{body}.json"
        ledger = read(path) if path.exists() else {"task_id": task, "body_id": body,
                    "max_corrections": 3, "corrections_reserved": 0, "reset_count": 0,
                    "initial_calls": 0, "last_raw": None}
        raw = ledger["last_raw"]
        if raw is None:
            ledger["initial_calls"] += 1
            write(path, ledger)
            raw = scorer._call(host, prompt)
            ledger["last_raw"] = raw
            write(path, ledger)
        while True:
            try:
                return scorer._parse(raw), ledger["initial_calls"] + ledger["corrections_reserved"]
            except Exception as exc:
                if ledger["corrections_reserved"] >= 3:
                    raise ValueError(f"Correction budget exhausted for task {task}/{body}") from exc
                ledger["corrections_reserved"] += 1
                write(path, ledger)
                raw = scorer._call(host, prompt + "\n\nFORMAT CORRECTION: Return only the required JSON object.")
                ledger["last_raw"] = raw
                write(path, ledger)
    scorer._review = review


def execute(args, root):
    identity = read(root / "alignment.json")
    if read(root / "status.json").get("status") == "complete":
        return
    with RunLock(root / "driver.pid"):
        reserve(root, args.stage, args.workers)
        (root / "PID").write_text(str(os.getpid()) + "\n")
        logging.basicConfig(filename=root / "relay.log", level=logging.INFO)
        relay = None
        try:
            if identity["executor_model"] == QWEN and not os.environ.get("TB21_EXECUTOR_API_KEY"):
                raise ValueError("E5 requires a verified Qwen credential in TB21_EXECUTOR_API_KEY")
            os.environ.update(MODEL_NAME=identity["executor_model"], TBENCH_PERSIST_SANDBOXES="0",
                TBENCH_TENCENT_ENV_FILE="/dev/null", EXPE_REVIEWER_RESPONSE_FORMAT="omit",
                EXPE_LLM_MAX_TOKENS="65536", EXPE_LLM_TIMEOUT_SECONDS="1800",
                EXPE_LLM_EXTRA_JSON='{"enable_thinking":true}')
            os.environ.pop("EXPE_LLM_DISABLE_THINKING", None)
            relay = relay_from_env()
            url = relay.start()
            # The relay retains its method credential; Harbor inherits the
            # independently authorized executor credential from this process.
            os.environ["TB21_METHOD_API_KEY"] = os.environ["OPENAI_API_KEY"]
            if identity["executor_model"] == QWEN and os.environ.get("TB21_EXECUTOR_API_KEY"):
                os.environ["OPENAI_API_KEY"] = os.environ["TB21_EXECUTOR_API_KEY"]
            write(root / "credential_roles.json", {"method": "desktop_deepseek_via_relay",
                  "executor": "existing_e1_qwen" if identity["executor_model"] == QWEN
                              and os.environ.get("TB21_EXECUTOR_API_KEY") else "desktop_deepseek",
                  "credentials_persisted": False})
            os.environ.update(EXPE_CONFIG_FILE=str(root / "config.json"), EXPE_TASK_FILE=str(args.task_file.resolve()),
                EXPE_LLM_BASE_URL=url, OPENAI_API_BASE=url, MODEL_API_BASE=url, EXPE_LLM_RELAY_REQUIRED="1")
            write(root / "relay_manifest.json", {"transport": "tencent_e2b_relay",
                  "sandbox_id": relay.transport.sandbox_id, "persistence": 0})
            proposal = root / "library_proposal/library_proposals.json"
            if not proposal.exists():
                subprocess.run([sys.executable, str(Path(__file__).with_name("propose_terminalbench_library.py")),
                                "--run-dir", str(root), "--model", METHOD], check=True)
            if not (root / "cold_start_complete.json").exists():
                subprocess.run([sys.executable, str(Path(__file__).with_name("materialize_terminalbench_library.py")),
                                "--run-dir", str(root), "--proposal", str(proposal)], check=True)
            if not read(root / "cold_start_complete.json").get("model_generated_library"):
                raise ValueError("Bootstrap cold start forbidden")
            cfg, plan, skills, _ = load_cold_start(root)
            tasks = read(root / "tasks.json")
            canaries = [(next(i for i, t in enumerate(tasks) if t["task_name"] == n), 1) for n in CANARIES]
            def batch(slots, phase):
                write(root / "status.json", {"stage": args.stage, "status": "running", "pid": os.getpid(),
                      "workers": args.workers, "phase": phase})
                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futures = [pool.submit(run_slot, root, cfg, skills, tasks, relay.transport, t, a,
                                           identity["executor_model"]) for t, a in slots]
                    for future in as_completed(futures):
                        future.result()
                        valid = sum(read(p).get("status") == "valid" for p in (root / "slots").glob("*/record.json"))
                        write(root / "status.json", {"stage": args.stage, "status": "running", "pid": os.getpid(),
                              "workers": args.workers, "phase": phase, "coverage": f"{valid}/267"})
            batch(canaries, "canary")
            if not all(read(root / f"slots/{t:02d}-{a}/record.json")["status"] == "valid" for t, a in canaries):
                raise ValueError("Canary failed; full panel locked")
            write(root / "canary.json", {"passed": True, "slots": canaries, "reused": True})
            batch([(t, a) for t in range(89) for a in (1, 2, 3) if (t, a) not in canaries], "skill_rollouts")
            collect_cards(root, tasks, skills, identity["executor_model"])
            # Ephemeral relay replacement is operational, not a new scoring protocol.
            if (root / "l2_manifest.json").exists():
                manifest = read(root / "l2_manifest.json")
                manifest["provider"] = provider_signature()
                write(root / "l2_manifest.json", manifest)
            write(root / "status.json", {"stage": args.stage, "status": "running", "pid": os.getpid(),
                  "workers": args.workers, "phase": "family_evolution", "coverage": "267/267"})
            loop = SerialEvolutionLoop(cfg, plan, LoopPaths(root), EvolutionConfig(
                batch_size=50, candidate_count=3, evolve_l1_workers=args.workers,
                l2_review_workers=args.workers, evolve_rounds=1, autonomous_attempts=3,
                supervised_attempts=0, skill_edit_mode="rewrite", acceptance_mode="predicted",
                predicted_review_scope="val", progressive_library=True, acceptance_panel="all_train"))
            install_review_budget(loop, root, tasks)
            result = loop.run_evolutions(1)
            if result["status"] != "complete" or result["invalid_batches"]:
                raise ValueError("Evolution journal incomplete")
            write(root / "result.json", result)
            write(root / "audit.json", {"stage": args.stage, "status": "complete", "raw_input": read(root / "input_audit.json"),
                  "execution": read(root / "execution_audit.json"), "round": read(root / "evolution/round-1/audit.json"),
                  "model_generated_library": True, "empirically_validated_final_library": False})
            (root / "report.md").write_text(f"# {args.stage} E1-Aligned Experiment\n\n"
                f"Run: `{root}`\n\n89-task closed-set; initial library freely generated from raw inputs.\n"
                f"267 valid initial-library executions; {result['review_approved_updates']} predicted-gate updates.\n"
                "Final library has no separate verifier evaluation; no independent test claim.\n"
                f"Final heads: `{json.dumps(result['skills'])}`\n")
            write(root / "status.json", {"stage": args.stage, "status": "complete", "workers": args.workers,
                  "coverage": "267/267", "review_approved_updates": result["review_approved_updates"]})
        except Exception as exc:
            message = str(exc)
            for key in ("OPENAI_API_KEY", "E2B_API_KEY", "TB21_METHOD_API_KEY", "TB21_EXECUTOR_API_KEY"):
                if os.environ.get(key):
                    message = message.replace(os.environ[key], "[REDACTED]")
            write(root / "status.json", {"stage": args.stage, "status": "needs_attention", "workers": args.workers,
                  "error_class": failure_class(exc), "error": message[:2000]})
            print(json.dumps({"status": "needs_attention", "error": message[:2000]}))
        finally:
            if relay:
                relay.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=("E3", "E5"))
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--deepseek-source", required=True, type=Path)
    parser.add_argument("--qwen-source", type=Path)
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--raw-gate", type=Path, default=Path("runs/tb21-raw-gate-qwen-accepted-20261008/audit.json"))
    parser.add_argument("--workers", type=int, default=42)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 42 or (args.stage == "E5" and args.qwen_source is None):
        parser.error("Each stage needs 1..42 workers; E5 needs --qwen-source")
    root = prepare(args)
    if not args.prepare_only:
        execute(args, root)


if __name__ == "__main__":
    main()
