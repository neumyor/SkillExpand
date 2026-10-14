"""Offline validation of the common library-pilot execution conditions."""

import copy
import importlib.util
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "runtime.py"
SPEC = importlib.util.spec_from_file_location("pilot_runtime_config_test", SOURCE)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def template():
    return {
        "job_name": "e10", "jobs_dir": "/old/jobs", "n_attempts": 3,
        "orchestrator": {"type": "local", "n_concurrent_trials": 100,
                         "retry": {"max_retries": 3, "min_wait_sec": 5}},
        "agents": [{"import_path": "e10_bare_environment:E10BareTerminus",
                    "model_name": "openai/qwen3.6-flash-distill", "override_timeout_sec": 7200,
                    "kwargs": {"api_base": "https://llm-center.modelbest.co/v1",
                               "interleaved_thinking": True,
                               "model_info": {"max_input_tokens": 262144, "max_output_tokens": 65536},
                               "enable_summarize": True, "proactive_summarization_threshold": 8000,
                               "llm_call_kwargs": {"top_p": .95, "max_tokens": 65536,
                                                   "extra_body": {"top_k": 20, "enable_thinking": True}}}}],
        "environment": {"import_path": "e10_bare_environment:E10BareEnvironment",
                        "delete": True, "kwargs": {"snapshot_retries": 3}},
        "datasets": [{"path": "/original/tasks", "task_names": None,
                      "exclude_task_names": None}],
        "verifier": {"disable": False, "override_timeout_sec": None},
    }


def test_template_is_not_mutated_and_existing_parameters_are_preserved(tmp_path):
    original = template()
    before = copy.deepcopy(original)
    cfg = runtime.build_config(original, tmp_path, tmp_path / "old/01", "cancel-async-tasks", tmp_path / "skills")
    assert original == before
    assert cfg["verifier"] == original["verifier"]
    assert cfg["agents"][0]["kwargs"]["llm_call_kwargs"] == original["agents"][0]["kwargs"]["llm_call_kwargs"]
    assert cfg["environment"]["kwargs"]["snapshot_retries"] == 3
    assert cfg["agents"][0]["override_timeout_sec"] == 7200
    cfg["agents"][0]["kwargs"]["llm_call_kwargs"]["extra_body"]["top_k"] = 0
    assert original == before


@pytest.mark.parametrize("phase", ["old", "candidate-1", "candidate-2", "final"])
def test_every_phase_uses_identical_context_provider_and_single_trial(tmp_path, phase):
    cfg = runtime.build_config(template(), tmp_path, tmp_path / phase / "01", "headless-terminal", tmp_path / phase / "skills")
    agent = cfg["agents"][0]
    assert agent["import_path"].endswith(":TencentSkillTerminus2")
    assert agent["model_name"] == "openai/qwen3.6-flash-distill"
    assert agent["kwargs"]["api_base"] == "https://llm-center.modelbest.co/v1"
    assert agent["kwargs"]["model_info"] == {"max_input_tokens": 262144, "max_output_tokens": 65536}
    assert agent["kwargs"]["enable_summarize"] is True
    assert agent["kwargs"]["proactive_summarization_threshold"] == 8000
    assert agent["kwargs"]["skills_dir"] == "/opt/openclaw-skills"
    assert cfg["n_attempts"] == cfg["orchestrator"]["n_concurrent_trials"] == 1
    assert cfg["orchestrator"]["retry"]["max_retries"] == 0
    assert cfg["jobs_dir"] == str(tmp_path / phase / "01/jobs")
    assert cfg["datasets"][0]["path"] == str(tmp_path / "frozen/tasks")
    assert cfg["datasets"][0]["task_names"] == ["headless-terminal"]


@pytest.mark.parametrize("command,expected", [
    ("/tests/test.sh", "/tests/test.sh < /dev/null"),
    ("/tests/test.sh --option", "/tests/test.sh --option < /dev/null"),
    ("/tests/test.sh-extra", "/tests/test.sh-extra"),
    ("echo /tests/test.sh", "echo /tests/test.sh"),
    ("python /app/solve.py", "python /app/solve.py"),
])
def test_stdin_adaptation_is_generic_and_only_matches_verifier(command, expected):
    assert runtime.verifier_command(command) == expected


def test_multi_dataset_or_agent_template_is_rejected(tmp_path):
    cfg = template()
    cfg["datasets"].append(copy.deepcopy(cfg["datasets"][0]))
    with pytest.raises(ValueError, match="one agent and one dataset"):
        runtime.build_config(cfg, tmp_path, tmp_path / "slot", "task", tmp_path / "skills")
