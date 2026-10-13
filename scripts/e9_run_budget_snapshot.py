#!/usr/bin/env python
"""AgentBrew on Terminal-Bench: fixed-order brew, frozen single-rollout serve."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
from dataclasses import asdict
from importlib.metadata import distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harbor.models.task.task import Task  # noqa: E402

from tbench_eval.agentbrew.adapter import RolloutAdapter  # noqa: E402
from tbench_eval.agentbrew.audit import audit_run  # noqa: E402
from tbench_eval.agentbrew.config import load_method  # noqa: E402
from tbench_eval.agentbrew.preflight import preflight  # noqa: E402
from tbench_eval.agentbrew.processes import resume_readiness, stop_rollout_children  # noqa: E402
from tbench_eval.agentbrew.retrieval import DenseEncoder, Retriever  # noqa: E402
from tbench_eval.agentbrew.runtime import Runtime  # noqa: E402
from tbench_eval.agentbrew.storage import digest, read, run_lock, save  # noqa: E402
from tbench_eval.agentbrew.tb21 import validate_snapshot  # noqa: E402
from tbench_eval.agentbrew.teacher import JSONTeacher  # noqa: E402
from tbench_eval.config import load_config  # noqa: E402
from tbench_eval.local_env import load_project_env  # noqa: E402
from tbench_eval.tasks import (  # noqa: E402
    load_manifest,
    load_split,  # noqa: E402
    verify_split_files,
)


def prepare(args):
    method = load_method(args.config)
    if os.environ.get("AGENTBREW_TEACHER_MODEL"):
        method.teacher.model = os.environ["AGENTBREW_TEACHER_MODEL"]
    if os.environ.get("AGENTBREW_TEACHER_API_BASE"):
        method.teacher.api_base = os.environ["AGENTBREW_TEACHER_API_BASE"]
    evaluate = load_config(method.eval_config)
    if evaluate.dataset.dataset_args().get("path") is None:
        raise ValueError("AgentBrew requires a pinned, runnable local task snapshot")
    if evaluate.agent.extra_kwargs.get("use_responses_api", False):
        raise ValueError("Responses API is not supported by this adapter")
    splits = {
        name: (read(ROOT / evaluate.dataset.splits_dir / f"{name}.json")["tasks"]
               if method.evaluation_protocol == "same_task_set"
               else load_split(ROOT / evaluate.dataset.splits_dir / f"{name}.json"))
        for name in ("evolve", "validation", "test")
    }
    manifest = load_manifest(ROOT / evaluate.dataset.manifest)
    if method.evaluation_protocol == "same_task_set":
        known = set(manifest.names)
        if (len(known) != 89 or set(splits["evolve"]) != known
                or set(splits["test"]) != known or splits["validation"]
                or method.serve_split != "test"):
            raise ValueError("same_task_set requires full 89-task evolve/test and empty validation")
        if any(len(names) != len(set(names)) for names in splits.values()):
            raise ValueError("duplicate split task")
    else:
        verify_split_files(
            manifest, {name: ROOT / evaluate.dataset.splits_dir / f"{name}.json" for name in splits}
        )
    snapshot = None
    if method.rollout_backend == "tb21-tencent":
        if manifest.dataset != "terminal-bench@2.1":
            raise ValueError("TB2.1 rollout cannot use a different benchmark manifest")
        snapshot = validate_snapshot(
            method.task_snapshot, evaluate.dataset.local_path, method.tencent_harness_path
        )
        if set(manifest.names) != set(snapshot["task_checksums"]):
            raise ValueError("TB2.1 manifest differs from the imported task snapshot")
    brew = args.brew_tasks if args.brew_tasks is not None else splits["evolve"]
    serve = args.serve_tasks if args.serve_tasks is not None else splits[method.serve_split]
    if len(set(brew)) != len(brew) or len(set(serve)) != len(serve):
        raise ValueError("duplicate requested task")
    if not set(brew) <= set(splits["evolve"]) or not set(serve) <= set(splits[method.serve_split]):
        raise ValueError("requested tasks are outside their registered split")
    if method.evaluation_protocol == "held_out" and set(brew) & set(serve):
        raise ValueError("brewing and serving tasks overlap")
    if method.evaluation_protocol == "same_task_set" and (set(brew) != set(manifest.names) or set(serve) != set(manifest.names)):
        raise ValueError("same_task_set requires all 89 tasks in both phases")
    assets = {}
    for name in dict.fromkeys(brew + serve):
        task = Task(Path(evaluate.dataset.local_path) / name)
        assets[name] = {"instruction": task.instruction, "checksum": task.checksum}
        if snapshot and task.checksum != snapshot["task_checksums"][name]:
            raise ValueError("TB2.1 executed task checksum differs from the snapshot")
    source = list((ROOT / "src/tbench_eval/agentbrew").glob("*.py")) + [
        ROOT / "src/tbench_eval/agents/agentbrew_terminus.py",
        ROOT / "src/tbench_eval/agents/agentbrew_tencent.py",
        Path(__file__),
        ROOT / "scripts/run_eval.py",
        ROOT / "scripts/run_agentbrew_remote.py",
        ROOT / "scripts/supervise.py",
    ]
    source.extend(
        ROOT / "src/tbench_eval" / name
        for name in (
            "config.py",
            "jobconfig.py",
            "results.py",
            "scorer.py",
            "tasks.py",
            "local_env.py",
            "env/e2b.py",
            "env/tencent_vpc_dns.py",
        )
    )
    if method.tencent_harness_path:
        source.extend(Path(method.tencent_harness_path).rglob("*.py"))
    dist = distribution("harbor")
    contract = {
        "method": method.model_dump(),
        "eval": asdict(evaluate),
        "brew_tasks": brew,
        "serve_tasks": serve,
        "serve_split": method.serve_split,
        "repeats": args.repeats,
        "with_vanilla": args.with_vanilla,
        "metric": "mean single-rollout success rate",
        "assets": {k: v["checksum"] for k, v in assets.items()},
        "dataset_manifest_sha256": hashlib.sha256(
            (ROOT / evaluate.dataset.manifest).read_bytes()
        ).hexdigest(),
        "split_hashes": {k: digest(v) for k, v in splits.items()},
        "source_hashes": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source
        },
        "harbor": json.loads(dist.read_text("direct_url.json") or "{}"),
        "tb21_snapshot_hash": digest(snapshot) if snapshot else None,
        "execution_identity": {
            "mode": method.execution_mode,
            "python_entrypoint": sys.executable,
            "python_prefix": sys.prefix,
            "python_version": sys.version,
            "owned_relay": json.loads(os.environ.get("AGENTBREW_EXECUTION_CONTRACT", "null")),
        },
    }
    return method, evaluate, assets, contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--brew-tasks", nargs="+")
    parser.add_argument("--serve-tasks", nargs="+")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--with-vanilla", action="store_true")
    parser.add_argument(
        "--live-sandbox", action="store_true", help="with preflight: one sandbox exec"
    )
    parser.add_argument(
        "--phase",
        choices=["all", "brew", "serve", "dry-run", "audit", "preflight", "readiness"],
        default="all",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    load_project_env()
    root = args.work_dir.resolve()
    if args.phase == "readiness":
        report = resume_readiness(root)
        print(json.dumps(report, indent=2))
        return 0 if report["safe_to_open_run"] else 1
    with run_lock(root):
        if args.phase == "audit":
            report = audit_run(root, scope="formal")
            print(json.dumps(report, indent=2))
            return 0 if report["passed"] else 1
        readiness = resume_readiness(root)
        if not readiness["safe_to_open_run"]:
            raise RuntimeError("tracked rollout children still active; refusing another driver")
        method, evaluate, tasks, contract = prepare(args)
        path = root / "contract.json"
        if path.exists():
            if not args.resume:
                raise ValueError("run already exists; use --resume with the same contract")
            if read(path) != contract:
                authorization = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/tb21-e9-agentbrew-evo89-test89-20261012/budget_reset_20261013.json')
                old = read(path)
                allowed = {'source_hashes', 'tb21_snapshot_hash', 'method', 'eval'}
                expected_method = json.loads(json.dumps(old['method']))
                expected_method['teacher'].update(
                    model='deepseek-v4-flash-0731', temperature=None,
                    max_input_tokens=1048576, max_tokens=65536,
                    extra_body={'top_p': 0.95, 'top_k': 20, 'enable_thinking': True,
                                'chat_template_kwargs': {'preserve_thinking': True},
                                'reasoning_effort': 'max'})
                # Disclosures may change; executable algorithm settings must match exactly.
                expected_method['implementation_choices'] = contract['method']['implementation_choices']
                expected_eval = json.loads(json.dumps(old['eval']))
                expected_eval['agent']['thinking'] = True
                settings_match = (contract['method'] == expected_method
                                  and contract['eval'] == expected_eval)
                changed = {key for key in set(old) | set(contract) if old.get(key) != contract.get(key)}
                if (not authorization.exists() or not read(authorization).get('user_authorized')
                        or not changed <= allowed or not settings_match):
                    raise ValueError("resume contract differs (code/config/tasks/budgets/splits)")
                save(root / 'contract.budget-reset-compatibility-20261013.json',
                     {'original': old, 'current': contract, 'changed_fields': sorted(changed),
                      'authorization_file': str(authorization), 'historical_requests_preserved': True,
                      'settings_authorization': 'User confirmed E8-aligned teacher and student thinking on 2026-10-13',
                      'historical_contract_retained': True})
                contract = old
        else:
            save(path, contract)
        if args.phase == "dry-run":
            print(
                json.dumps(
                    {
                        "contract_hash": digest(contract),
                        "brew_tasks": contract["brew_tasks"],
                        "serve_tasks": contract["serve_tasks"],
                        "choices": method.implementation_choices,
                    },
                    indent=2,
                )
            )
            return 0
        encoder = DenseEncoder(method.retrieval.embedding_model, method.retrieval.embedding_cache)
        if args.phase == "preflight":
            report = preflight(
                method, evaluate, encoder, root, contract["brew_tasks"][0], args.live_sandbox
            )
            print(json.dumps(report, indent=2))
            return 0 if report["healthy"] else 1
        if args.phase != "serve" and not os.environ.get(method.teacher.api_key_env):
            raise ValueError(f"missing model credential variable {method.teacher.api_key_env}")
        runtime_class = Runtime
        if method.execution_mode == "tb_batch_notes":
            from tbench_eval.agentbrew.batch_runtime import BatchRuntime

            runtime_class = BatchRuntime
        runtime = runtime_class(
            root,
            method,
            contract,
            tasks,
            Retriever(method.retrieval, method.scopes, encoder),
            JSONTeacher(method.teacher, evaluate.agent.model_name, method.scopes),
            RolloutAdapter(
                method.eval_config,
                method.infra_attempts,
                rollout_backend=method.rollout_backend,
                harness_path=method.tencent_harness_path,
                task_failure_policy=method.task_failure_policy,
            ),
        )
        migration_path = root / 'contract.budget-reset-compatibility-20261013.json'
        if migration_path.exists():
            migration = read(migration_path)
            if migration['original'] != contract:
                raise ValueError('settings migration does not match historical contract')
            runtime.adapter.legacy_eval_contracts = [contract['eval']]
            runtime.adapter.execution_contract_hash = digest(migration['current'])

        def terminate(_signum, _frame):
            # Cancel before raising: ThreadPoolExecutor drains its running
            # workers, which otherwise could wait a full task deadline.
            runtime.adapter.cancel_all()
            stop_rollout_children(root)
            raise KeyboardInterrupt

        previous_handlers = {
            sig: signal.signal(sig, terminate) for sig in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            if args.phase in {"all", "brew"}:
                runtime.brew()
            if args.phase in {"all", "serve"}:
                runtime.serve(args.repeats)
            report = audit_run(root, scope="brew" if args.phase == "brew" else "formal")
            print(json.dumps(report, indent=2))
            return 0 if report["passed"] else 1
        finally:
            try:
                runtime.adapter.cancel_all()
                stop_rollout_children(root)
                runtime.write_costs()
            finally:
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
