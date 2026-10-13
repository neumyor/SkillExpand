"""One fresh, isolated Harbor trial per physical rollout attempt."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Lock

from tbench_eval.config import load_config
from tbench_eval.results import load_attempts, reward_from_result, validate_attempts

from .errors import (
    ConfigurationError,
    EvidenceError,
    InvalidContentError,
    OutputBudgetExceeded,
    TransportExhaustedError,
)
from .processes import process_identity, process_matches, stop_owned_process
from .storage import digest, read, run_lock, save
from .verifier import checked_result, verifier_evidence

BENCH = Path(__file__).resolve().parents[3]
AGENT = "tbench_eval.agents.agentbrew_terminus:AgentBrewTerminus2"
TB21_AGENT = "tbench_eval.agents.agentbrew_tencent:AgentBrewTencentTerminus2"
DISK_EXHAUSTION_MARK = "No space left on device"


def _host_disk_exhausted(physical):
    """True when the trial's own driver log proves host-side disk exhaustion.

    Only positive ENOSPC evidence counts; an operator interrupting a healthy
    launcher leaves no such trace and never takes this path.
    """
    log = Path(physical) / "driver.log"
    try:
        return DISK_EXHAUSTION_MARK in log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _trial_teardown_write_failed(physical):
    """True when a trial's result and snapshot writes both failed as zero-byte files.

    Harbor writes both at trial teardown; an empty pair means teardown itself
    died on a failed write -- e.g. the disk filled mid-write and truncated the
    driver log before the errno digits were recorded.
    """
    for result in (Path(physical) / "jobs").glob("*/*/result.json"):
        try:
            read(result)
        except (json.JSONDecodeError, OSError):
            snapshot = result.parent / "snapshot.json"
            try:
                if snapshot.is_file() and not snapshot.read_text():
                    return True
            except OSError:
                return False
    return False


class RolloutAdapter:
    def __init__(
        self,
        eval_config,
        infra_attempts,
        *,
        rollout_backend="local",
        harness_path=None,
        task_failure_policy="block",
    ):
        self.eval_config = str(Path(eval_config).resolve())
        self.config = load_config(self.eval_config)
        self.infra_attempts = infra_attempts
        self.rollout_backend = rollout_backend
        self.harness_path = Path(harness_path).resolve() if harness_path else None
        self.agent_import_path = TB21_AGENT if rollout_backend == "tb21-tencent" else AGENT
        self._cancelled = Event()
        self._child_lock = Lock()
        self._children = {}
        self._admission_lock = Lock()
        self._next_admission = 0.0
        self.legacy_eval_contracts = []
        self.execution_contract_hash = None
        if task_failure_policy not in {"block", "terminal"}:
            raise ValueError("invalid task failure policy")
        self.task_failure_policy = task_failure_policy
        if rollout_backend == "tb21-tencent" and self.harness_path is None:
            raise ValueError("TB2.1 Tencent rollout requires a harness path")
        if self.config.skill.mode != "none" or self.config.runner.max_trial_retries != 0:
            raise ValueError("AgentBrew eval config requires skill none and max_trial_retries=0")

    def command(self, physical, unit, request):
        return [
            sys.executable,
            str(BENCH / "scripts/run_eval.py"),
            "--config",
            self.eval_config,
            "--tasks",
            request["task"],
            "--skill-mode",
            "none",
            "--attempts",
            "1",
            "--n-concurrent",
            "1",
            "--agent-import-path",
            self.agent_import_path,
            "--agent-kwargs",
            json.dumps(
                {
                    "memory_request_path": str((unit / "request.json").resolve()),
                    "memory_request_sha256": digest(request),
                }
            ),
            "--out",
            str(physical / "eval"),
            "--jobs-dir",
            str(physical / "jobs"),
        ]

    def child_env(self, env):
        env.update(self.config.environment.runner_env())
        if self.harness_path is not None:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(x for x in (str(self.harness_path), existing) if x)
            env["HARBOR_E2B_TEMPLATE"] = self.config.environment.e2b_template
            env["HARBOR_E2B_FS_USER"] = "root"
            env["TBENCH_PERSIST_SANDBOXES"] = "0"
            if env.get("E2B_API_KEY", "").startswith("ark_"):
                env["E2B_VALIDATE_API_KEY"] = "false"
        return env

    def run(self, unit, request):
        with run_lock(Path(unit) / "adapter_owner"):
            return self._run(unit, request)

    def cancel_all(self):
        """Stop dispatch and terminate precisely registered rollout sessions."""
        self._cancelled.set()
        with self._child_lock:
            children = list(self._children.values())
        for identity in children:
            stop_owned_process(identity)

    def _run(self, unit, request):
        unit = Path(unit)
        signature = digest({"request": request, "eval": read_eval_contract(self.eval_config)})
        completed = unit / "rollout.json"
        if completed.exists():
            result = read(completed)
            permitted = {signature} | {
                digest({"request": request, "eval": previous})
                for previous in self.legacy_eval_contracts
            }
            if result["signature"] not in permitted:
                raise ValueError("rollout signature changed")
            self._validate_saved(result, request)
            if self.config.runner.verifier_evidence_policy != "pytest":
                return result
            source = Path(
                result.get("source_physical_result_path")
                or (Path(result["raw_result_path"]).parents[3] / "result.json")
            )
            checked = checked_result(result, source)
            status = checked.get("verifier_evidence", {}).get("status")
            if status == "tests_executed" or result.get("terminal_failure"):
                return result
            if status not in {"preparation_failed", "agent_failed"}:
                raise EvidenceError("cached verifier outcome lacks test execution evidence")
            backup = completed.with_name("rollout_before_network_fix.json")
            if backup.exists():
                raise EvidenceError("cached verifier recovery already has an original artifact")
            completed.replace(backup)
            if status == "agent_failed":
                self._validate_saved(checked, request)
                save(completed, checked)
                return checked
        save(unit / "request.json", request)
        key = os.environ.get(self.config.agent.api_key_env)
        if not key:
            raise ValueError(f"missing model credential variable {self.config.agent.api_key_env}")
        env = dict(os.environ)
        env[self.config.agent.agent_key_env] = key
        env = self.child_env(env)
        extra = int(os.environ.get("TB21_AUTHORIZED_INFRA_EXTRA_ATTEMPTS", "0"))
        if extra not in (0, 2):
            raise ValueError("Unsupported authorized infrastructure budget amendment")
        for attempt in range(1, self.infra_attempts + extra + 1):
            if self._cancelled.is_set():
                raise TransportExhaustedError("rollout cancellation prevents new dispatch")
            physical = unit / f"{self.config.runner.physical_attempt_prefix}_{attempt:02d}"
            intent = physical / "intent.json"
            status = physical / "result.json"
            if status.exists():
                result = read(status)
            elif intent.exists():
                if read(intent) != {"signature": signature, "request_hash": digest(request)}:
                    raise EvidenceError("pending rollout intent changed")
                child_file = physical / "child.json"
                if child_file.exists():
                    if process_matches(read(child_file)):
                        raise EvidenceError(
                            "tracked rollout child is still alive; do not duplicate it"
                        )
                result = self._read_physical(physical, request, signature, interrupted=True)
                save(status, result)
            else:
                physical.mkdir(parents=True, exist_ok=True)
                save(intent, {"signature": signature, "request_hash": digest(request)})
                code = self._execute(physical, unit, request, env)
                result = self._read_physical(physical, request, signature, exit_code=code)
                if code == 2:
                    result.update(infra=False, failure_kind="configuration_invalid")
                save(status, result)
            if result.get("signature") != signature:
                raise EvidenceError("physical rollout signature changed")
            self._refuse_verifier_resampling(result)
            if not result["infra"]:
                result = self._finalize_execution_failure(result, status)
                self._require_outcome(result)
                self._validate_saved(result, request)
                save(completed, result)
                return result
        raise TransportExhaustedError(f"infrastructure budget exhausted at {unit}")

    def _execute(self, physical, unit, request, env):
        with self._admission_lock:
            delay = max(0.0, self._next_admission - time.monotonic())
            if self._cancelled.wait(delay):
                raise TransportExhaustedError("rollout interrupted during admission")
            self._next_admission = time.monotonic() + self.config.runner.physical_start_interval_sec
        with (physical / "driver.log").open("ab") as log:
            with self._child_lock:
                if self._cancelled.is_set():
                    raise TransportExhaustedError("rollout interrupted before dispatch")
                child = subprocess.Popen(
                    self.command(physical, unit, request),
                    cwd=BENCH,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                identity = process_identity(child.pid) or {"pid": child.pid}
                self._children[child.pid] = identity
                save(physical / "child.json", identity)
            try:
                return child.wait()
            except BaseException:
                stop_owned_process(identity)
                child.wait()
                raise
            finally:
                with self._child_lock:
                    self._children.pop(child.pid, None)

    def _finalize_execution_failure(self, result, source):
        if self.task_failure_policy != "terminal":
            return result
        return self._execution_failure(result, source)

    @staticmethod
    def _execution_failure(result, source):
        if result.get("infra") or result.get("raw_reward") is not None:
            return result
        if result.get("failure_kind") not in {
            "unknown_exception",
            "invalid_content",
            "output_budget_exhausted",
            "context_budget_exhausted",
        }:
            return result
        if not result.get("raw_result_path"):
            return result
        raw_path = Path(result["raw_result_path"])
        raw = read(raw_path)
        if not raw.get("exception_info") or raw.get("verifier_result") is not None:
            return result
        return {
            **result,
            "reward": 0,
            "reported_reward": 0,
            "verifier_reward": None,
            "terminal_failure": True,
            "teaching_eligible": False,
            "outcome_source": "agent_execution_failure",
            "failure_kind": "agent_execution_failure",
            "original_failure_kind": result["failure_kind"],
            "source_physical_result_path": str(source),
        }

    @staticmethod
    def _refuse_verifier_resampling(result):
        if result.get("infra") and result.get("raw_reward") in (0, 1):
            evidence = result.get("verifier_evidence")
            if (
                evidence
                and evidence == verifier_evidence(result["raw_result_path"])
                and (evidence["status"] == "preparation_failed")
            ):
                return
            raise EvidenceError("binary verifier outcome conflicts with infrastructure failure")

    @staticmethod
    def _require_outcome(result):
        classification = result.get("failure_kind")
        if classification == "configuration_invalid":
            raise ConfigurationError("student rollout configuration rejected")
        if classification in {"evidence_invalid", "unknown_exception"}:
            raise EvidenceError("student rollout has a nonretryable unknown/evidence exception")
        if result.get("reward") not in (0, 1):
            if classification in {"output_budget_exhausted", "context_budget_exhausted"}:
                raise OutputBudgetExceeded("student model budget exhausted without verifier reward")
            raise InvalidContentError(
                "student rollout lacks a valid verifier outcome; no resampling"
            )

    def _read_physical(self, physical, request, signature, exit_code=None, interrupted=False):
        files = []
        for path in (physical / "jobs").glob("*/*/result.json"):
            try:
                read(path)
            except (json.JSONDecodeError, OSError):
                # A zero-byte or corrupt trial result carries no evidence; the
                # trial stays unsettled instead of failing the resume.
                continue
            files.append(path)
        result = {
            "signature": signature,
            "task": request["task"],
            "request_hash": digest(request),
            "infra": True,
            "reward": None,
            "raw_reward": None,
            "exit_code": exit_code,
            "interrupted": interrupted,
            "physical_dir": str(physical),
            "usage": None,
        }
        if self.execution_contract_hash:
            result["execution_contract_hash"] = self.execution_contract_hash
        if len(files) > 1:
            raise EvidenceError("duplicate trial results; cannot resample")
        if not files:
            # Missing verifier artifacts cannot turn a completed model failure
            # into a new sample. Preserve native terminal response evidence.
            result.update(
                infra=interrupted,
                failure_kind=("infra_retryable" if interrupted else "evidence_invalid"),
                issue="missing trial result",
            )
            disk_exhausted = interrupted and (
                _host_disk_exhausted(physical) or _trial_teardown_write_failed(physical)
            )
            for log in (physical / "jobs").glob("**/agent/raw_model_responses.jsonl"):
                lines = [
                    line for line in log.read_text(errors="replace").splitlines() if line.strip()
                ]
                if not lines:
                    continue
                try:
                    last = json.loads(lines[-1])
                except json.JSONDecodeError:
                    # The final response record itself was cut mid-write; no
                    # completed response evidence exists in this log.
                    continue
                raw_response = last.get("response") or last.get("raw") or last
                choices = raw_response.get("choices") or []
                if choices:
                    choice = choices[0]
                    if choice.get("finish_reason") == "length":
                        result.update(infra=False, failure_kind="output_budget_exhausted")
                    elif not (choice.get("message") or {}).get("content"):
                        result.update(infra=False, failure_kind="invalid_content")
                    elif disk_exhausted:
                        # Host disk exhaustion killed the harness before any
                        # settle; the completed response never reached
                        # verification, and re-running yields this unit's
                        # first scored sample. Original evidence is preserved.
                        result.update(issue="host disk exhaustion interrupted the trial")
                    else:
                        # A completed response without final verifier evidence
                        # is unresolved. An interrupted launcher does not
                        # authorize rerunning that completed model interaction.
                        result.update(infra=False, failure_kind="evidence_invalid")
                elif last.get("http_status") == 200:
                    result.update(infra=False, failure_kind="invalid_content")
            return result
        raw_path = files[0]
        raw = read(raw_path)
        rows = load_attempts(raw_path.parent.parent)
        validate_attempts(rows, expected_tasks=[request["task"]], expected_attempts=1)
        row = rows[0]
        if raw["task_checksum"] != request["task_checksum"]:
            raise ValueError("executed task assets differ from registered task checksum")
        agent_result = raw.get("agent_result") or {}
        result.update(
            infra=row.infrastructure_failure,
            reward=row.reward,
            raw_reward=reward_from_result(raw),
            exception=row.exception_type,
            failure_kind=getattr(row, "failure_kind", None),
            trial_id=row.trial_id,
            raw_result_path=str(raw_path),
            trajectory_path=row.trajectory_path,
            usage={
                k: agent_result.get(k)
                for k in ("n_input_tokens", "n_output_tokens", "n_cache_tokens", "cost_usd")
            },
        )
        if self.rollout_backend == "tb21-tencent":
            # The native backend uses cost_usd=0 as a placeholder. It also
            # retries HTTP calls internally; sum durable physical responses
            # rather than reporting only Harbor's successful logical calls.
            response_log = raw_path.parent / "agent/raw_model_responses.jsonl"
            responses = (
                [json.loads(line) for line in response_log.read_text().splitlines() if line]
                if response_log.is_file()
                else []
            )
            usages = [row.get("usage") for row in responses]
            # The native backend wraps exhausted HTTP errors in RuntimeError.
            # Require durable transport evidence, never its name alone.
            if result["reward"] is None and result["exception"] == "RuntimeError" and responses:
                last = responses[-1]
                status_code = last.get("http_status")
                if status_code in {408, 409, 425, 429, 500, 502, 503, 504, 529} or (
                    status_code == 0 and last.get("transport_error")
                ):
                    result.update(infra=True, failure_kind="infra_retryable")
                elif isinstance(status_code, int) and 400 <= status_code < 500:
                    result.update(infra=False, failure_kind="configuration_invalid")
            result["native_http_attempts"] = len(responses)
            result["usage"]["cost_usd"] = None
            result["usage"]["n_input_tokens"] = (
                sum(usage.get("prompt_tokens", 0) for usage in usages if usage) if usages else None
            )
            result["usage"]["n_output_tokens"] = (
                sum(usage.get("completion_tokens", 0) for usage in usages if usage)
                if usages
                else None
            )
            result["transport_usage_complete"] = bool(usages) and all(
                usage and "prompt_tokens" in usage and "completion_tokens" in usage
                for usage in usages
            )
        if self.config.runner.verifier_evidence_policy == "pytest":
            result = checked_result(result, physical / "result.json")
        return result

    @staticmethod
    def _validate_saved(result, request):
        if (
            result["request_hash"] != digest(request)
            or result["infra"]
            or result["reward"] not in (0, 1)
        ):
            raise ValueError("invalid completed rollout")
        raw_path = Path(result["raw_result_path"])
        raw = read(raw_path)
        if raw["task_checksum"] != request["task_checksum"]:
            raise ValueError("completed rollout task checksum differs")
        rows = load_attempts(raw_path.parent.parent)
        validate_attempts(rows, expected_tasks=[request["task"]], expected_attempts=1)
        if result.get("outcome_source") == "agent_verifier_setup_failure":
            source_path = Path(result["source_physical_result_path"])
            source = read(source_path)
            if checked_result(source, source_path) != result:
                raise ValueError("agent preparation failure differs from original evidence")
        elif result.get("terminal_failure"):
            source_path = Path(result["source_physical_result_path"])
            source = read(source_path)
            if (
                RolloutAdapter._execution_failure(source, source_path) != result
                or rows[0].infrastructure_failure
                or rows[0].raw_reward is not None
                or source.get("reward") != rows[0].reward
                or source.get("exception") != rows[0].exception_type
            ):
                raise ValueError("terminal agent failure differs from original exception evidence")
        elif rows[0].infrastructure_failure or rows[0].reward != result["reward"]:
            raise ValueError(
                "completed rollout reward or infrastructure status differs from raw evidence"
            )
        if result.get("verifier_evidence"):
            if result["verifier_evidence"] != verifier_evidence(raw_path):
                raise ValueError("verifier execution evidence changed")
            if not result.get("terminal_failure") and (
                result["verifier_evidence"]["status"] != "tests_executed"
            ):
                raise ValueError("completed rollout lacks executed test evidence")
        evidence = read(raw_path.parent / "agent/agentbrew_memory.json")
        if evidence["request_hash"] != digest(request) or evidence["request"] != request:
            raise ValueError("student memory evidence mismatch")
        requests = sorted((raw_path.parent / "agent/agentbrew_requests").glob("*.json"))
        budget_path = raw_path.parent / "agent/agentbrew_memory_budget.json"
        pre_execution_overflow = (
            result.get("terminal_failure")
            and result.get("original_failure_kind") == "context_budget_exhausted"
            and not requests
            and budget_path.is_file()
        )
        if pre_execution_overflow:
            budget = read(budget_path)
            if (
                budget.get("fits_context") is not False
                or budget["initial_input_tokens"] + budget["reserved_output_tokens"]
                <= budget["model_context_limit"]
            ):
                raise ValueError("pre-execution context failure lacks budget evidence")
        if (not requests and not pre_execution_overflow) or not Path(
            result["trajectory_path"] or ""
        ).is_file():
            raise ValueError("valid student rollout lacks request/trajectory evidence")
        for path in requests:
            record = read(path)
            system = [x for x in record["messages"] if x["role"] == "system"]
            expected = request["retrieval"]["rendered_system"]
            if system != ([{"role": "system", "content": expected}] if expected else []):
                raise ValueError("actual model memory messages differ from planned injection")
        native_requests = raw_path.parent / "agent/raw_model_requests.jsonl"
        if native_requests.is_file():
            effective = [read(path)["messages"] for path in requests]
            rows, truncated = [], 0
            for line in native_requests.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # The native harness can fail writing a large request record
                    # mid-line (observed sandbox-side truncation at ~372 KB); a
                    # partial record carries no verifiable payload. Tolerated
                    # for validation, disclosed by the final audit scan.
                    truncated += 1
            if truncated and not rows:
                raise ValueError("native request log carries no verifiable records")
            if len({row["request_id"] for row in rows}) != len(rows):
                raise ValueError("duplicate native TB2.1 request identity")
            if any(row["messages"] not in effective for row in rows):
                raise ValueError("native TB2.1 provider payload differs from memory evidence")


def read_eval_contract(path):
    from dataclasses import asdict

    return asdict(load_config(path))
