"""Frozen library-pilot Harbor configuration and generic Tencent adapters."""

import copy
import json
import os
from pathlib import Path
import re
import runpy
import sys


MODULE_NAME = "library_pilot_runtime"


def build_config(template, root, slot, task, skill_root):
    """Derive one fresh trial without modifying the E10 template."""
    if not isinstance(task, str) or not task or Path(task).name != task or task in (".", ".."):
        raise ValueError("Expected a single task name")
    cfg = copy.deepcopy(template)
    if len(cfg["agents"]) != 1 or len(cfg["datasets"]) != 1:
        raise ValueError("The pilot requires one agent and one dataset")
    root, slot = Path(root).resolve(), Path(slot).resolve()
    cfg.update(job_name="library_pilot_trial", jobs_dir=str(slot / "jobs"), n_attempts=1)
    cfg["orchestrator"]["n_concurrent_trials"] = 1
    cfg["orchestrator"]["retry"]["max_retries"] = 0
    agent = cfg["agents"][0]
    agent["import_path"] = MODULE_NAME + ":TencentSkillTerminus2"
    agent["kwargs"].update(
        model_info={"max_input_tokens": 262144, "max_output_tokens": 65536},
        enable_summarize=True,
        proactive_summarization_threshold=8000,
        skills_dir="/opt/openclaw-skills",
        record_terminal_session=False,
        record_terminal_output=False,
    )
    cfg["environment"]["import_path"] = MODULE_NAME + ":LibraryPilotEnvironment"
    cfg["environment"]["kwargs"]["skill_root"] = str(Path(skill_root).resolve())
    cfg["datasets"][0].update(path=str(root / "frozen/tasks"), task_names=[task])
    return cfg


def verifier_command(command):
    """Close stdin only for the generic official verifier entrypoint."""
    return command + " < /dev/null" if re.match(r"^/tests/test\.sh(?:\s|$)", command) else command


def _load_adapters():
    from tencent_tb2_skill_env import TencentTaskSkillE2BEnvironment
    from tencent_sandbox_terminus import TencentSkillTerminus2 as BaseTencentSkillTerminus2

    class LibraryPilotEnvironment(TencentTaskSkillE2BEnvironment):
        async def start(self, force_build):
            await super().start(force_build)
            mirror = os.environ.get("LIBRARY_PILOT_MIRROR", "https://mirrors.tencentyun.com")
            if mirror:
                from tencent_package_mirrors import configure
                paths = await configure(self._sandbox, mirror)
                (self.trial_paths.trial_dir / "package_network.json").write_text(
                    json.dumps({"dns": "183.60.83.19", "mirror": mirror,
                                "configured_paths": paths}) + "\n", encoding="utf-8"
                )

        async def exec(self, command, **kwargs):
            return await super().exec(verifier_command(command), **kwargs)

    class TencentSkillTerminus2(BaseTencentSkillTerminus2):
        async def setup(self, environment):
            await super().setup(environment)
            source, target = environment._task_skill_source()
            (self.logs_dir / "effective_agent_config.json").write_text(
                json.dumps({
                    "max_input_tokens": self._llm.get_model_context_limit(),
                    "max_output_tokens": self._llm.get_model_output_limit(),
                    "enable_summarize": self._enable_summarize,
                    "proactive_summarization_threshold": self._proactive_summarization_threshold,
                    "api_base": self._tencent_api_base,
                    "skills_dir": self._selected_skills_dir,
                    "skill_source": str(source / "SKILL.md"),
                    "skill_target": str(target / "SKILL.md"),
                }) + "\n", encoding="utf-8"
            )

    globals().update(LibraryPilotEnvironment=LibraryPilotEnvironment,
                     TencentSkillTerminus2=TencentSkillTerminus2)


def __getattr__(name):
    if name in ("LibraryPilotEnvironment", "TencentSkillTerminus2"):
        _load_adapters()
        return globals()[name]
    raise AttributeError(name)


def main():
    root = Path(__file__).resolve().parent
    sys.path[:0] = [str(root), str(root / "frozen/runtime/tencent/python"), str(root / "frozen/runtime/framework")]
    sys.modules[MODULE_NAME] = sys.modules[__name__]
    from functools import partial
    import e2b.sandbox_async.main as sdk
    from e2b.api.client_async import get_envd_transport

    sdk.get_transport = partial(get_envd_transport, http2=False)
    # CLI arguments stay intact for run_tencent_job's parser.
    runpy.run_path(str(root / "frozen/runtime/tencent/python/run_tencent_job.py"), run_name="__main__")


if __name__ == "__main__":
    main()
