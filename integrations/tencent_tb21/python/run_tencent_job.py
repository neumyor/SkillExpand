"""Run a Harbor job while preserving failed retry executions."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import shutil
from pathlib import Path
from typing import Any

import yaml
from harbor.cli.jobs import print_job_results_tables
from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.models.trial.result import ExceptionInfo
from harbor.trial.hooks import TrialHookEvent
from harbor.utils.traces_utils import export_traces

logger = logging.getLogger("tencent_tb21_runner")


def _handle_sigterm(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def patch_result_agent_name(trial_dir: Path) -> None:
    result_path = trial_dir / "result.json"
    if not result_path.is_file():
        return
    data = json.loads(result_path.read_text())
    config = data.get("config") or {}
    agent = config.get("agent")
    if isinstance(agent, dict) and not agent.get("name"):
        agent["name"] = "terminus-2"
        _write_json(result_path, data)


def archive_failed_execution(
    trial_dir: Path,
    archive_root: Path,
    run_id: str,
    trial_name: str,
    exception_type: str,
) -> Path:
    destination_root = archive_root / run_id / trial_name
    destination_root.mkdir(parents=True, exist_ok=True)
    next_number = len(list(destination_root.glob("execution-*"))) + 1
    destination = destination_root / f"execution-{next_number:02d}"
    temporary = destination_root / f".execution-{next_number:02d}.tmp"
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(trial_dir, temporary)
    snapshot_path = temporary / "snapshot.json"
    _write_json(
        temporary / "archive_record.json",
        {
            "trial_name": trial_name,
            "execution": next_number,
            "exception_type": exception_type,
            "snapshot_recorded": snapshot_path.is_file(),
        },
    )
    temporary.replace(destination)
    return destination


def fail_fast_category(exception: ExceptionInfo | None) -> str | None:
    """Return an actionable shared-service failure category, if any."""
    if exception is None:
        return None
    text = "\n".join(
        (
            exception.exception_type,
            exception.exception_message,
            exception.exception_traceback,
        )
    ).lower()
    if "connectioninputs.send_settings" in text and "connectionstate.closed" in text:
        if os.getenv("TBENCH_FAIL_FAST_CONNECTION_ERRORS", "1") != "1":
            return None
        return "tencent_e2b_connection_closed"
    if any(term in text for term in ("authentication", "unauthorized", "http 401", "http 403")):
        return "authentication"
    if "tencent sandbox model request failed after retries" in text:
        return "model_retries_exhausted"
    return None


async def run_job(
    config_path: Path,
    archive_root: Path,
    export: bool,
    fail_fast: bool = False,
) -> int:
    config = JobConfig.model_validate(yaml.safe_load(config_path.read_text()))
    job = Job(config)
    from tencent_pipeline_cache import enabled, on_verification_started, on_trial_ended
    if enabled():
        job.on_verification_started(on_verification_started)
        job.on_trial_ended(on_trial_ended)
    failures: dict[str, set[str]] = {}
    stop_reason: dict[str, Any] | None = None
    job_task: asyncio.Task[Any] | None = None

    async def preserve_retry(event: TrialHookEvent) -> None:
        trial_dir = Path(event.config.trials_dir) / event.trial_id
        patch_result_agent_name(trial_dir)
        if event.result is None or event.result.exception_info is None:
            return
        try:
            await asyncio.to_thread(
                archive_failed_execution,
                trial_dir=trial_dir,
                archive_root=archive_root,
                run_id=config.job_name,
                trial_name=event.trial_id,
                exception_type=event.result.exception_info.exception_type,
            )
        except Exception:
            logger.exception("Failed to archive execution for %s", event.trial_id)

    job.on_trial_ended(preserve_retry)
    if fail_fast:
        async def stop_on_shared_failure(event: TrialHookEvent) -> None:
            nonlocal stop_reason
            if stop_reason is not None or event.result is None:
                return
            category = fail_fast_category(event.result.exception_info)
            if category is None:
                return
            tasks = failures.setdefault(category, set())
            tasks.add(event.task_name)
            if len(tasks) < 3:
                return
            stop_reason = {
                "status": "stopped",
                "category": category,
                "task_names": sorted(tasks),
                "threshold": 3,
            }
            _write_json(job.job_dir / "fail_fast.json", stop_reason)
            logger.error("Fail-fast triggered: %s", stop_reason)
            if job_task is not None:
                job_task.cancel()

        job.on_trial_ended(stop_on_shared_failure)

    job_task = asyncio.create_task(job.run())
    try:
        result = await job_task
    except asyncio.CancelledError:
        if stop_reason is None:
            raise
        print(f"[fail-fast] Stopped {config.job_name}: {stop_reason['category']}")
        return 2
    print_job_results_tables(result)
    if export:
        try:
            traces = export_traces(job.job_dir, recursive=True, episodes="all")
            count = len(traces.get("main", [])) if isinstance(traces, dict) else len(traces)
            print(f"[traces] Exported {count} rows from {job.job_dir}")
        except Exception:
            logger.exception("Trace export failed for %s", job.job_dir)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--retry-archive-root", required=True, type=Path)
    parser.add_argument("--no-export-traces", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            run_job(
                args.config,
                args.retry_archive_root,
                not args.no_export_traces,
                fail_fast=args.fail_fast,
            )
        )
    )


if __name__ == "__main__":
    main()
