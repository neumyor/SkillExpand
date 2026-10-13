from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpcore
from h2.exceptions import ProtocolError
os.environ.setdefault("E2B_API_KEY", "ark_test_key")
os.environ.setdefault("OPENAI_API_KEY", "test-model-key")

import tencent_tb2_snapshot as snapshot_module
from run_tencent_job import (
    archive_failed_execution,
    fail_fast_category,
    patch_result_agent_name,
)
from tencent_sandbox_terminus import (
    E2BSandboxLLM,
    TencentSandboxTerminus2,
    TencentSkillTerminus2,
)
from tencent_tb2_snapshot import TencentSnapshotE2BEnvironment
from tencent_tb2_skill_env import TencentTaskSkillE2BEnvironment


class FakeSandbox:
    def __init__(self, failures: int = 0) -> None:
        self.sandbox_id = "sandbox-test"
        self.failures = failures
        self.pause_calls: list[bool] = []

    async def pause(self, keep_memory: bool = True) -> bool:
        self.pause_calls.append(keep_memory)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("pause failed")
        return True


def make_environment(tmp_path: Path, sandbox: FakeSandbox, retries: int = 3):
    environment = TencentSnapshotE2BEnvironment.__new__(TencentSnapshotE2BEnvironment)
    environment._sandbox = sandbox
    environment._snapshot_retries = retries
    environment.environment_name = "fix-git"
    environment.session_id = "session-test"
    environment.trial_paths = SimpleNamespace(trial_dir=tmp_path / "job-test" / "trial-test")
    environment.logger = logging.getLogger("tencent-adapter-test")
    return environment


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_exec_kills_owned_command(self):
        handle = SimpleNamespace(wait=AsyncMock(side_effect=asyncio.CancelledError()),
                                 kill=AsyncMock())
        environment = TencentSnapshotE2BEnvironment.__new__(TencentSnapshotE2BEnvironment)
        environment._sandbox = SimpleNamespace(commands=SimpleNamespace(run=AsyncMock(return_value=handle)))
        environment._fs_user = 'root'
        environment._workdir = '/app'
        environment.task_env_config = SimpleNamespace(workdir='/app')
        environment._resolve_user = lambda user: user
        environment._merge_env = lambda env: env or {}
        with self.assertRaises(asyncio.CancelledError):
            await environment.exec('sleep 100')
        handle.kill.assert_awaited_once()

    def test_ark_key_disables_public_sdk_shape_validation(self) -> None:
        self.assertEqual(os.environ["E2B_VALIDATE_API_KEY"], "false")

    async def test_stop_pauses_filesystem_and_does_not_kill_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = FakeSandbox()
            environment = make_environment(Path(directory), sandbox)
            await environment.stop(delete=True)

            record = json.loads(
                (Path(directory) / "job-test" / "trial-test" / "snapshot.json").read_text()
            )
            self.assertEqual(sandbox.pause_calls, [False])
            self.assertEqual(record["status"], "paused_filesystem")
            self.assertEqual(record["persistent_sandbox_id"], "sandbox-test")
            self.assertEqual(record["job_id"], "job-test")
            self.assertEqual(record["trial_id"], "trial-test")
            self.assertIsNone(environment._sandbox)

    async def test_start_prepares_harbor_log_directories_as_root(self) -> None:
        environment = TencentSnapshotE2BEnvironment.__new__(TencentSnapshotE2BEnvironment)
        environment.exec = AsyncMock(
            return_value=SimpleNamespace(stdout="", stderr="", return_code=0)
        )
        with patch.object(
            snapshot_module._BaseE2BEnvironment, "start", new=AsyncMock()
        ) as base_start, patch('tencent_environment_setup.prepare_environment', new=AsyncMock()) as setup:
            await environment.start(force_build=False)
        setup.assert_awaited_once_with(environment)
        base_start.assert_awaited_once_with(False)
        command = environment.exec.await_args.args[0]
        self.assertIn("/logs/agent", command)
        self.assertIn("/logs/verifier", command)
        self.assertIn("/logs/artifacts", command)
        self.assertEqual(environment.exec.await_args.kwargs["user"], "root")

    async def test_pause_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = FakeSandbox(failures=1)
            environment = make_environment(Path(directory), sandbox)
            with patch.object(snapshot_module.asyncio, "sleep", new=AsyncMock()):
                record = await environment._create_persistent_snapshot()
            self.assertEqual(record["attempts"], 2)
            self.assertEqual(sandbox.pause_calls, [False, False])

    async def test_pause_failure_keeps_sandbox_reference_without_failing_trial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = FakeSandbox(failures=1)
            environment = make_environment(Path(directory), sandbox, retries=1)
            await environment.stop(delete=True)
            self.assertIs(environment._sandbox, sandbox)
            record = json.loads(
                (Path(directory) / "job-test" / "trial-test" / "snapshot.json").read_text()
            )
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["source_sandbox_id"], "sandbox-test")

    async def test_capture_pane_reconnects_after_remote_protocol_error(self) -> None:
        successful_handle = SimpleNamespace(
            wait=AsyncMock(
                return_value=SimpleNamespace(stdout="screen", stderr="", exit_code=0)
            )
        )
        class ReconnectableSandbox:
            replacement = None
            reconnect = AsyncMock()

            def __init__(self, commands):
                self.sandbox_id = "sandbox-test"
                self.commands = commands

            @classmethod
            async def _cls_connect_sandbox(cls, *args, **kwargs):
                await cls.reconnect(*args, **kwargs)
                return cls.replacement

        original = ReconnectableSandbox(
            SimpleNamespace(
                run=AsyncMock(side_effect=httpcore.RemoteProtocolError("closed"))
            )
        )
        replacement = SimpleNamespace(
            sandbox_id="sandbox-test",
            commands=SimpleNamespace(run=AsyncMock(return_value=successful_handle)),
        )
        ReconnectableSandbox.replacement = replacement
        environment = TencentSnapshotE2BEnvironment.__new__(
            TencentSnapshotE2BEnvironment
        )
        environment._sandbox = original
        environment._fs_user = "root"
        environment._workdir = "/app"
        environment.task_env_config = SimpleNamespace(workdir="/app")
        environment.logger = logging.getLogger("tencent-adapter-test")
        environment._resolve_user = lambda user: user
        environment._merge_env = lambda env: env or {}

        result = await environment.exec("tmux capture-pane -p -t agent")

        self.assertEqual(result.stdout, "screen")
        ReconnectableSandbox.reconnect.assert_awaited_once()
        replacement.commands.run.assert_awaited_once()

    async def test_exec_reconnects_after_http2_protocol_error(self) -> None:
        successful_handle = SimpleNamespace(
            wait=AsyncMock(
                return_value=SimpleNamespace(stdout="screen", stderr="", exit_code=0)
            )
        )

        class ReconnectableSandbox:
            replacement = None

            def __init__(self, commands):
                self.sandbox_id = "sandbox-test"
                self.commands = commands

            @classmethod
            async def _cls_connect_sandbox(cls, *_args, **_kwargs):
                return cls.replacement

        original = ReconnectableSandbox(
            SimpleNamespace(run=AsyncMock(side_effect=ProtocolError("closed")))
        )
        replacement = ReconnectableSandbox(
            SimpleNamespace(run=AsyncMock(return_value=successful_handle))
        )
        ReconnectableSandbox.replacement = replacement
        environment = TencentSnapshotE2BEnvironment.__new__(
            TencentSnapshotE2BEnvironment
        )
        environment._sandbox = original
        environment._fs_user = "root"
        environment._workdir = "/app"
        environment.task_env_config = SimpleNamespace(workdir="/app")
        environment.logger = logging.getLogger("tencent-adapter-test")
        environment._resolve_user = lambda user: user
        environment._merge_env = lambda env: env or {}

        result = await environment.exec("echo ok")

        self.assertEqual(result.stdout, "screen")
        replacement.commands.run.assert_awaited_once()


class FakeFiles:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def write(self, path: str, value: bytes, **_: object) -> None:
        self.values[path] = value

    async def read(self, path: str, **_: object) -> bytes:
        return self.values[path]

    async def make_dir(self, path: str, **_: object) -> None:
        self.values.setdefault(path.rstrip("/") + "/", b"")


class TaskSkillEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_mounts_only_matching_skill_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_root = Path(directory) / "skills"
            task_skill = skill_root / "fix-git"
            (task_skill / "scripts").mkdir(parents=True)
            (task_skill / "SKILL.md").write_text("# fix-git\n")
            (task_skill / "scripts" / "recover.sh").write_text("#!/bin/sh\n")

            environment = TencentTaskSkillE2BEnvironment.__new__(
                TencentTaskSkillE2BEnvironment
            )
            environment._skill_root = skill_root.resolve()
            environment._skill_mount_root = Path("/opt/openclaw-skills")
            environment.environment_name = "fix-git"
            environment._sandbox = SimpleNamespace(files=FakeFiles())
            environment.exec = AsyncMock(
                return_value=SimpleNamespace(stdout="", stderr="", return_code=0)
            )

            await environment._mount_task_skill()

            values = environment._sandbox.files.values
            self.assertIn("/opt/openclaw-skills/fix-git/SKILL.md", values)
            self.assertIn("/opt/openclaw-skills/fix-git/scripts/recover.sh", values)
            self.assertFalse(any("other-task" in path for path in values))
            self.assertIn("chmod -R a-w", environment.exec.await_args.args[0])

    def test_known_skill_directory_override_keeps_task_mount_name(self) -> None:
        environment = TencentTaskSkillE2BEnvironment.__new__(
            TencentTaskSkillE2BEnvironment
        )
        environment._skill_root = Path("/skills")
        environment._skill_mount_root = Path("/opt/openclaw-skills")
        environment.environment_name = "install-windows-3.11"
        with patch.object(Path, "is_dir", return_value=True), patch.object(
            Path, "is_symlink", return_value=False
        ), patch.object(Path, "is_file", return_value=True), patch.object(
            Path, "rglob", return_value=[]
        ):
            source, target = environment._task_skill_source()
        self.assertEqual(source, Path("/skills/install-windows-3-11"))
        self.assertEqual(target, Path("/opt/openclaw-skills/install-windows-3.11"))


class SkillTerminusTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_agent_appends_read_instruction(self) -> None:
        agent = TencentSkillTerminus2.__new__(TencentSkillTerminus2)
        agent._skill_read_suffix = "Please read the skill first."
        with patch.object(TencentSandboxTerminus2, "run", new=AsyncMock()) as run:
            await agent.run("Solve the task.", SimpleNamespace(), SimpleNamespace())
        self.assertEqual(
            run.await_args.args[0],
            "Solve the task.\n\nPlease read the skill first.",
        )


class FakeModelEnvironment:
    def __init__(self, responses: list[tuple[int, dict]] | None = None) -> None:
        self._sandbox = SimpleNamespace(files=FakeFiles())
        self.exec_calls: list[tuple[str, dict[str, str] | None, str | int | None]] = []
        self.responses = responses or [
            (
                200,
                {
                    "id": "response-test",
                    "model": "qwen-test",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "done", "reasoning_content": "reason"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "prompt_tokens_details": {"cached_tokens": 1},
                    },
                },
            )
        ]
        self.model_attempt = 0

    async def exec(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ):
        self.exec_calls.append((command, env, user))
        if command.startswith("python3 -c "):
            self.model_attempt += 1
            request_path = next(
                path for path in self._sandbox.files.values if "tb21-model-request-" in path
            )
            response_path = request_path.replace(
                "model-request", "model-response"
            ).replace(".json", f"-{self.model_attempt}.json")
            status, response = self.responses[self.model_attempt - 1]
            envelope = {
                "status": status,
                "retry_after": None,
                "transport_error": None,
                "body": json.dumps(response),
            }
            self._sandbox.files.values[response_path] = json.dumps(envelope).encode()
            return SimpleNamespace(stdout="", stderr="", return_code=0)
        return SimpleNamespace(stdout="", stderr="", return_code=0)


class SandboxLLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_call_runs_in_sandbox_and_writes_raw_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = FakeModelEnvironment()
            llm = E2BSandboxLLM(
                environment=environment,
                logs_dir=Path(directory),
                model_name="openai/qwen-test",
                api_base="https://llm-center.modelbest.co/v1",
                max_input_tokens=1024,
                max_output_tokens=128,
                llm_call_kwargs={"top_p": 0.9},
            )
            response = await llm.call("hello", [{"role": "system", "content": "test"}])

            command, env, user = environment.exec_calls[0]
            self.assertIn("urllib.request.urlopen", command)
            self.assertNotIn("test-model-key", command)
            self.assertEqual(env, {"OPENAI_API_KEY": "test-model-key"})
            self.assertEqual(user, "root")
            self.assertEqual(response.content, "done")
            self.assertEqual(response.reasoning_content, "reason")
            self.assertTrue((Path(directory) / "raw_model_requests.jsonl").is_file())
            self.assertTrue((Path(directory) / "raw_model_responses.jsonl").is_file())

    async def test_retryable_http_response_is_logged_before_success(self) -> None:
        success = FakeModelEnvironment().responses[0][1]
        environment = FakeModelEnvironment(
            responses=[
                (429, {"error": {"code": "rate-limit"}}),
                (200, success),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            llm = E2BSandboxLLM(
                environment=environment,
                logs_dir=Path(directory),
                model_name="openai/qwen-test",
                api_base="https://llm-center.modelbest.co/v1",
                max_input_tokens=1024,
                max_output_tokens=128,
            )
            with patch("tencent_sandbox_terminus.asyncio.sleep", new=AsyncMock()) as sleep:
                response = await llm.call("hello")
            records = [
                json.loads(line)
                for line in (Path(directory) / "raw_model_responses.jsonl").read_text().splitlines()
            ]
            self.assertEqual([record["http_status"] for record in records], [429, 200])
            self.assertEqual([record["attempt"] for record in records], [1, 2])
            sleep.assert_awaited_once_with(5.0)
            self.assertEqual(response.content, "done")

    async def test_server_error_is_retried(self) -> None:
        success = FakeModelEnvironment().responses[0][1]
        environment = FakeModelEnvironment(
            responses=[(503, {"error": {"code": "unavailable"}}), (200, success)]
        )
        with tempfile.TemporaryDirectory() as directory:
            llm = E2BSandboxLLM(
                environment=environment,
                logs_dir=Path(directory),
                model_name="openai/qwen-test",
                api_base="https://llm-center.modelbest.co/v1",
                max_input_tokens=1024,
                max_output_tokens=128,
            )
            with patch("tencent_sandbox_terminus.asyncio.sleep", new=AsyncMock()):
                await llm.call("hello")
            records = [
                json.loads(line)
                for line in (Path(directory) / "raw_model_responses.jsonl").read_text().splitlines()
            ]
            self.assertEqual([record["http_status"] for record in records], [503, 200])


class RetryArchiveTests(unittest.TestCase):
    def test_fail_fast_recognizes_shared_tencent_connection_failure(self) -> None:
        error = SimpleNamespace(
            exception_type="RuntimeError",
            exception_message="Invalid input ConnectionInputs.SEND_SETTINGS in state ConnectionState.CLOSED",
            exception_traceback="",
        )
        self.assertEqual(fail_fast_category(error), "tencent_e2b_connection_closed")

    def test_fail_fast_recognizes_exhausted_model_retries(self) -> None:
        error = SimpleNamespace(
            exception_type="RuntimeError",
            exception_message="Tencent sandbox model request failed after retries: {}",
            exception_traceback="",
        )
        self.assertEqual(fail_fast_category(error), "model_retries_exhausted")

    def test_failed_execution_is_archived_before_retry_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root / "jobs" / "run-test" / "fix-git__trial"
            (trial / "agent").mkdir(parents=True)
            (trial / "agent" / "trajectory.json").write_text('{"steps": []}\n')
            (trial / "snapshot.json").write_text('{"status": "paused_filesystem"}\n')
            (trial / "result.json").write_text(
                json.dumps({"config": {"agent": {"name": None}}}) + "\n"
            )

            patch_result_agent_name(trial)
            destination = archive_failed_execution(
                trial,
                root / "archives",
                "run-test",
                "fix-git__trial",
                "RuntimeError",
            )

            self.assertTrue((destination / "agent" / "trajectory.json").is_file())
            self.assertTrue((destination / "snapshot.json").is_file())
            self.assertEqual(
                json.loads((trial / "result.json").read_text())["config"]["agent"]["name"],
                "terminus-2",
            )
            archive = json.loads((destination / "archive_record.json").read_text())
            self.assertTrue(archive["snapshot_recorded"])


if __name__ == "__main__":
    unittest.main()
