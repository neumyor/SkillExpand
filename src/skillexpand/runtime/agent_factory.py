
"""Configuration loading and construction of executors and reasoning hosts."""
import os
from typing import Any, Dict, List, Optional

from omegaconf import OmegaConf

from skillexpand.runtime.prompts.templates.system import system_message_prompt
from skillexpand.runtime.prompts import SYSTEM_INSTRUCTION
from skillexpand.runtime.prompts import HUMAN_INSTRUCTION
from skillexpand.runtime.prompts import FEWSHOTS
from skillexpand.runtime.prompts import RULE_TEMPLATE
from skillexpand.runtime.prompts import LLM_PARSER
from skillexpand.runtime.prompts import OBSERVATION_FORMATTER
from skillexpand.benchmarks import ENVS
from skillexpand.benchmarks import INIT_TASKS_FN
from skillexpand.runtime.models import LLM_CLS
from skillexpand.reliability.errors import InvalidInput


def load_config(benchmark: str = 'alfworld', agent: str = 'expel') -> Any:
    """Reproduce hydra's config composition without hydra's ``@main`` decorator.

    Mirrors ``configs/train.yaml``'s defaults block plus the two interpolations
    (``ai_name: ${benchmark.ai_name}``, ``agent_type: ${agent.name}``), so the
    resulting object is interchangeable with the ``cfg`` the entry points see.

    Two upstream defaults are overridden, both because this deployment differs
    from the paper's.  Each override is deliberate:

    *   ``agent.llm`` -- upstream ships ``gpt-3.5-turbo``.  Pointing
        ``EXPE_LLM_BASE_URL`` at a local server is not enough: the *name* is sent
        verbatim in the request, so the server answers
        ``The model `gpt-3.5-turbo` does not exist``.  The name comes from
        ``EXPE_LLM_MODEL`` instead.

    *   ``benchmark.env.show_admissible_commands`` -- upstream never shows the
        engine's valid action set, which costs a 7B executor most of its
        episodes (72% of failures: invalid action -> "Nothing happens." ->
        repeated action -> immediate termination).  Enabled by default here and
        applied identically to every arm; set ``EXPE_SHOW_ADMISSIBLE=0`` to
        recover ExpeL's original, pristine observation for a comparison run.
    """
    import os

    from pathlib import Path
    config_root = Path(__file__).resolve().parents[1] / 'configs'
    bench = OmegaConf.load(config_root / 'benchmark' / f'{benchmark}.yaml')
    ag = OmegaConf.load(config_root / 'agent' / f'{agent}.yaml')

    remote_model = os.environ.get('EXPE_LLM_MODEL')
    if os.environ.get('EXPE_LLM_BASE_URL'):
        ag.llm = remote_model or 'alfworld-rl'

    bench.env.show_admissible_commands = (
        os.environ.get('EXPE_SHOW_ADMISSIBLE', '1').lower()
        not in ('0', 'false', 'no', 'off'))

    # Which task list the whole run indexes into.  It has to travel as an environment
    # variable rather than an argument: the spawned unit workers rebuild the config
    # themselves, and a task id means nothing without the list it indexes.
    task_file = os.environ.get('EXPE_TASK_FILE')
    if task_file:
        bench.task_file = task_file

    return OmegaConf.create({
        'benchmark': bench,
        'agent': ag,
        'models': {
            'l1_executor': ag.llm,
            'cold_start': ag.llm,
            'l2_planner': ag.llm,
            'l2_editor': ag.llm,
            'l2_reviewer': ag.llm,
            'selector': ag.llm,
        },
        'ai_name': bench.ai_name,
        'agent_type': ag.name,
        'log_dir': 'logs',
        'run_name': 'run',
        'testing': False,
        'resume': False,
        'no_rules': False,
    })


def build_agent(
    cfg: Any,
    *,
    agent_cls: Any,
    task_idx: int = 0,
    rules: Optional[str] = None,
    no_rules: Optional[bool] = None,
    openai_api_key: Optional[str] = None,
):
    """Build one executor for ``task_idx`` with the Skill body ``rules`` injected.

    ``rules`` is the *only* channel through which a Skill reaches the executor.
    The static benchmark few-shots are part of every prompt; nothing is
    retrieved from other tasks' trajectories.
    """
    tasks = INIT_TASKS_FN[cfg.benchmark.name](cfg)
    if not 0 <= task_idx < len(tasks):
        raise IndexError(f'task_idx {task_idx} outside 0..{len(tasks) - 1}')

    # A default placeholder key is not safe: a self-hosted vLLM ignores the key,
    # but a hosted gateway answers HTTP 401 -- and the failure surfaces hours
    # later as a mid-run crash rather than at construction.
    if openai_api_key is None:
        openai_api_key = os.environ.get('OPENAI_API_KEY') or ''
        if not openai_api_key:
            from skillexpand.runtime.models.llm import get_llm_base_url
            if get_llm_base_url():
                raise InvalidInput(
                    'no API key: OPENAI_API_KEY is unset but EXPE_LLM_BASE_URL '
                    'points at a hosted endpoint. Configure .env from '
                    '.env.example and source scripts/env.sh.')
            openai_api_key = 'EMPTY'

    agent = agent_cls(
        name=cfg.ai_name,
        system_instruction=SYSTEM_INSTRUCTION[cfg.benchmark.name],
        human_instruction=HUMAN_INSTRUCTION[cfg.benchmark.name],
        tasks=tasks,
        fewshots=FEWSHOTS[cfg.benchmark.name],
        system_prompt=system_message_prompt,
        env=ENVS[cfg.benchmark.name],
        max_steps=cfg.benchmark.max_steps,
        openai_api_key=openai_api_key,
        llm=cfg.agent.llm,
        llm_builder=LLM_CLS,
        rule_template=RULE_TEMPLATE[cfg.benchmark.name],
        llm_parser=LLM_PARSER[cfg.benchmark.name],
        observation_formatter=OBSERVATION_FORMATTER[cfg.benchmark.name],
        task_idx=task_idx,
        benchmark_name=cfg.benchmark.name,
    )

    # An EMPTY skill body must map to no_rules=True, not to injecting an empty
    # rules block.  RULE_TEMPLATE renders '' as "The following are some experience
    # you gather on a similar task ... Use these as references" followed by
    # nothing, which claims experience exists and perturbs the prompt relative to
    # vanilla.  A skill that has not been promoted yet must behave exactly like
    # vanilla -- that is the correct null state of the library.
    has_skill = bool(rules)
    if no_rules is None:
        no_rules = not has_skill
    agent.no_rules = bool(no_rules)
    agent.rules = rules if has_skill else ''
    return agent


#: Per-process cache of the benchmark's task table.
#:
#: ``INIT_TASKS_FN['alfworld']`` re-reads and re-parses the 81 KB task file on every
#: call, and a layer-1 wave asks for the text of every task it is about to dispatch.
#: The table is a pure function of the config, so caching it is safe inside a worker
#: process and saves one file parse per task.
_TASK_TABLE_CACHE: Dict[str, List[Dict[str, Any]]] = {}
def task_table(cfg: Any, refresh: bool = False) -> List[Dict[str, Any]]:
    """The benchmark's task entries, cached by benchmark, input path and task prefix."""
    name = (cfg.benchmark.name, str(cfg.benchmark.task_file), str(cfg.benchmark.task_prefix))
    if refresh or name not in _TASK_TABLE_CACHE:
        _TASK_TABLE_CACHE[name] = list(INIT_TASKS_FN[cfg.benchmark.name](cfg))
    return _TASK_TABLE_CACHE[name]


def task_text_of(cfg: Any, task_id: int) -> str:
    """The task's instruction, without the ``___N`` bookkeeping suffix.

    This is what the skill selector sees, and it must be the instruction alone: the
    suffix is an index into ExpeL's own task list, and leaking it into the selection
    prompt would hand the model a task identifier it could match against a skill's
    provenance rather than against the task.
    """
    text = task_table(cfg)[task_id]['task']
    # SearchQA clues legitimately contain blanks such as "____ Jesus".
    # Only ALFWorld attaches the bookkeeping suffix described above.
    if cfg.benchmark.name == 'alfworld':
        import re
        return re.sub(r'___\d+$', '', text)
    return text


def role_model(cfg, role: str) -> str:
    """Return a role-specific reasoning model, falling back to the executor."""
    models = cfg.get('models', {})
    value = models.get(role) if models else None
    return str(value or cfg.agent.llm)


def build_reasoning_host(cfg,usage_path=None, model=None, role=None):
    """A model/prompt host without constructing or reading a task environment."""
    from types import SimpleNamespace
    import tiktoken
    key=os.environ.get('OPENAI_API_KEY','')
    if not key:
        raise InvalidInput('OPENAI_API_KEY is required')
    selected = model or (role_model(cfg, role) if role else cfg.agent.llm)
    llm=LLM_CLS(llm_name=selected,openai_api_key=key)
    if usage_path:
        from skillexpand.persistence.usage import attach_usage
        attach_usage([llm],usage_path)
    tokenizer=tiktoken.get_encoding('cl100k_base')
    return SimpleNamespace(llm=llm,token_counter=lambda text:len(tokenizer.encode(text)),
                           benchmark_name=cfg.benchmark.name)
