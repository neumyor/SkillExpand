"""Tencent E2B environment with a persistent post-trial filesystem."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import httpcore
import httpx
from e2b.sandbox.commands.command_handle import CommandExitException
from harbor.environments.base import ExecResult
from h2.exceptions import ProtocolError


T = TypeVar("T")
_E2B_CONNECTION_ERRORS = (
    httpcore.RemoteProtocolError,
    httpcore.LocalProtocolError,
    httpcore.WriteError,
    httpx.RemoteProtocolError,
    httpx.LocalProtocolError,
    httpx.WriteError,
    ProtocolError,
)


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# AsyncSandbox.create shares the SDK's control-plane HTTP/2 client.  Staggering
# setup prevents its connection pool from entering the closed state under a
# 100-trial Harbor admission burst.  This does not limit already running trials.
_SANDBOX_START_GATE = asyncio.Semaphore(_positive_int_env("TBENCH_E2B_START_CONCURRENCY", 4))

# This protects the Tencent E2B control plane while leaving Harbor's active
# agent/model work unconstrained. In particular, CommandHandle.wait() happens
# outside this gate so long-lived agent commands do not consume an RPC slot.
_E2B_RPC_GATE = asyncio.Semaphore(_positive_int_env("TBENCH_E2B_RPC_CONCURRENCY", 12))
_E2B_RECONNECT_BACKOFF_SECONDS = (0.5, 1.0)

# Tencent AGS control-plane keys use the ``ark_`` prefix rather than the public
# E2B ``e2b_`` format. This disables only the SDK's local shape check; the
# Tencent service still authenticates the key.
if os.getenv("E2B_API_KEY", "").startswith("ark_"):
    os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")

try:
    from harbor_e2b_modelbest import E2BEnvironment as _BaseE2BEnvironment
except ImportError as exc:
    raise ImportError(
        "Tencent enterprise adapter harbor_e2b_modelbest is required; add its directory to PYTHONPATH"
    ) from exc


class TencentSnapshotE2BEnvironment(_BaseE2BEnvironment):
    """Persist the E2B filesystem before Harbor destroys a sandbox."""

    def __init__(self, *args, snapshot_retries: int = 3, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._snapshot_retries = max(1, int(snapshot_retries))
        self._fs_user = os.getenv("HARBOR_E2B_FS_USER", "root")
        # Formal TB2 runs use ephemeral task sandboxes. Retention is explicit
        # opt-in so an omitted variable cannot consume paused-sandbox quota.
        self._persist_sandboxes = os.getenv("TBENCH_PERSIST_SANDBOXES", "0") == "1"
        from tencent_pipeline_cache import register_environment
        register_environment(self)

    def _snapshot_path(self) -> Path:
        return self.trial_paths.trial_dir / "snapshot.json"

    def _write_snapshot_record(self, payload: dict[str, Any]) -> None:
        path = self._snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _trial_metadata(self) -> dict[str, str]:
        trial_dir = self.trial_paths.trial_dir
        return {
            "job_id": trial_dir.parent.name,
            "trial_id": trial_dir.name,
        }

    async def _reconnect_sandbox(self, timeout_sec: int | None = None) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            raise RuntimeError("Sandbox not found while reconnecting")
        for attempt in range(1, 4):
            try:
                async with _E2B_RPC_GATE:
                    self._sandbox = await type(sandbox)._cls_connect_sandbox(
                        sandbox.sandbox_id,
                        timeout=timeout_sec or 900,
                        logger=self.logger,
                    )
                return
            except _E2B_CONNECTION_ERRORS:
                if attempt == 3:
                    raise
                await asyncio.sleep(_E2B_RECONNECT_BACKOFF_SECONDS[attempt - 1])

    async def run_with_e2b_reconnect(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        operation_name: str,
        timeout_sec: int | None = None,
    ) -> T:
        """Retry an E2B RPC after replacing a closed HTTP/2 connection."""
        for attempt in range(1, 4):
            try:
                async with _E2B_RPC_GATE:
                    return await operation()
            except _E2B_CONNECTION_ERRORS:
                if attempt == 3:
                    raise
                self.logger.warning(
                    "Tencent E2B connection closed during %s; reconnecting (%s/3)",
                    operation_name,
                    attempt,
                )
                await asyncio.sleep(_E2B_RECONNECT_BACKOFF_SECONDS[attempt - 1])
                await self._reconnect_sandbox(timeout_sec)
        raise AssertionError("unreachable")

    async def start(self, force_build: bool) -> None:
        async with _SANDBOX_START_GATE:
            await super().start(force_build)
            result = await self.exec(
                "mkdir -p /logs/agent /logs/verifier /logs/artifacts && "
                "chmod 777 /logs/agent /logs/verifier /logs/artifacts",
                user="root",
            )
        if result.return_code != 0:
            raise RuntimeError(
                f"Failed to prepare Harbor log directories: {result.stderr or result.stdout}"
            )

    async def _create_persistent_snapshot(self) -> dict[str, Any]:
        sandbox = getattr(self, "_sandbox", None)
        if sandbox is None:
            raise RuntimeError("E2B sandbox is unavailable for snapshot creation")
        last_error: Exception | None = None
        for attempt in range(1, self._snapshot_retries + 1):
            try:
                await sandbox.pause(keep_memory=False)
                return {
                    "status": "paused_filesystem",
                    "artifact_type": "e2b_paused_sandbox",
                    "persistent_sandbox_id": sandbox.sandbox_id,
                    "source_sandbox_id": getattr(sandbox, "sandbox_id", None),
                    "environment_name": self.environment_name,
                    "session_id": self.session_id,
                    **self._trial_metadata(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "attempts": attempt,
                }
            except Exception as exc:  # SDK/provider failures are retried uniformly.
                last_error = exc
                if attempt < self._snapshot_retries:
                    await asyncio.sleep(min(attempt, 3))
        raise RuntimeError(
            f"E2B filesystem persistence failed after {self._snapshot_retries} attempts: {last_error}"
        ) from last_error

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        """Use the 539641 exec behavior without assuming task config.workdir."""
        from tencent_pipeline_cache import verifier_command_env
        env = verifier_command_env(self, command, env)
        resolved_user = self._resolve_user(user)
        merged_env = self._merge_env(env)
        if self._sandbox is None:
            raise RuntimeError("Sandbox not found. Please start the environment first.")
        run_kwargs = {
            "cmd": command,
            "background": True,
            "cwd": cwd or getattr(self.task_env_config, "workdir", None) or self._workdir,
            "envs": merged_env,
            "timeout": timeout_sec or 4 * 60 * 60,
            "user": str(resolved_user) if resolved_user is not None else self._fs_user,
        }
        async def start_command():
            if self._sandbox is None:
                raise RuntimeError("Sandbox not found while starting command")
            return await self._sandbox.commands.run(**run_kwargs)

        handle = await self.run_with_e2b_reconnect(
            start_command,
            operation_name="commands.run",
            timeout_sec=timeout_sec,
        )
        try:
            result = await handle.wait()
        except CommandExitException as exc:
            result = exc
        return ExecResult(
            stdout=result.stdout,
            stderr=result.stderr,
            return_code=result.exit_code,
        )

    async def stop(self, delete: bool):
        """Pause first; never kill a successfully persisted sandbox."""
        if not delete or getattr(self, "_sandbox", None) is None:
            return await super().stop(delete)
        if not getattr(self, "_persist_sandboxes", True):
            self._write_snapshot_record(
                {
                    "status": "deleted",
                    "artifact_type": "none",
                    "environment_name": self.environment_name,
                    "source_sandbox_id": getattr(self._sandbox, "sandbox_id", None),
                    "session_id": self.session_id,
                    **self._trial_metadata(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "persistent sandbox retention disabled",
                }
            )
            return await super().stop(delete)
        try:
            record = await self._create_persistent_snapshot()
            self._write_snapshot_record(record)
        except Exception as exc:
            self._write_snapshot_record(
                {
                    "status": "failed",
                    "environment_name": self.environment_name,
                    "session_id": self.session_id,
                    "source_sandbox_id": getattr(self._sandbox, "sandbox_id", None),
                    **self._trial_metadata(),
                    "error": str(exc),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self.logger.error(
                "E2B persistence failed; preserving sandbox for inspection without "
                "failing the trial: %s",
                exc,
            )
            return
        self.logger.info(
            "Persisted E2B filesystem in paused sandbox %s",
            record["persistent_sandbox_id"],
        )
        self._sandbox = None
