
import os
import json
from typing import Any, Callable, Dict, List, Optional

from omegaconf import OmegaConf

# ExpeL packages
from skillexpand.runtime.agent import AGENT
from skillexpand.runtime.prompts.templates.system import system_message_prompt
from skillexpand.runtime.prompts.templates.human import HUMAN_CRITIQUES
from skillexpand.runtime.prompts import SYSTEM_INSTRUCTION
from skillexpand.runtime.prompts import HUMAN_INSTRUCTION
from skillexpand.runtime.prompts import FEWSHOTS
from skillexpand.runtime.prompts import REFLECTION_FEWSHOTS
from skillexpand.runtime.prompts import HUMAN_REFLECTION_INSTRUCTION
from skillexpand.runtime.prompts import SYSTEM_REFLECTION_INSTRUCTION
from skillexpand.runtime.prompts import SYSTEM_CRITIQUE_INSTRUCTION
from skillexpand.runtime.prompts import RULE_TEMPLATE
from skillexpand.runtime.prompts import LLM_PARSER
from skillexpand.runtime.prompts import OBSERVATION_FORMATTER
from skillexpand.runtime.prompts import STEP_IDENTIFIER
from skillexpand.runtime.prompts import CYCLER
from skillexpand.runtime.prompts import STEP_CYCLER
from skillexpand.runtime.prompts import REFLECTION_PREFIX
from skillexpand.runtime.prompts import PREVIOUS_TRIALS_FORMATTER
from skillexpand.runtime.prompts import STEP_STRIPPER
from skillexpand.runtime.prompts import CRITIQUE_SUMMARY_SUFFIX
from skillexpand.benchmarks import ENVS
from skillexpand.benchmarks import INIT_TASKS_FN
from skillexpand.runtime.memory import RETRIEVERS
from skillexpand.runtime.models import LLM_CLS
from skillexpand.runtime.utils import get_fewshot_max_tokens

from skillexpand.runtime import embedders

from functools import partial

#: fewshot_strategy value that disables ExpeL's trajectory retrieval entirely.
FEWSHOT_NONE = 'none'


def load_config(benchmark: str = 'alfworld', agent: str = 'expel') -> Any:
    """Reproduce hydra's config composition without hydra's ``@main`` decorator.

    Mirrors ``configs/train.yaml``'s defaults block plus the two interpolations
    (``ai_name: ${benchmark.ai_name}``, ``agent_type: ${agent.name}``), so the
    resulting object is interchangeable with the ``cfg`` the entry points see.

    Two upstream defaults are overridden, both because this deployment differs
    from the paper's.  Each override is deliberate and logged:

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

    *   ``agent.retrieval_kwargs.embedder_type`` -- upstream ships
        ``huggingface``, which makes every agent construction download a ~420 MB
        SentenceTransformer checkpoint.  This experiment must not pull weights
        onto the local host, so the zero-download lexical backend is the default.
        See :mod:`skillexpand.runtime.embedders`.
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

    ag.retrieval_kwargs.embedder_type = embedders.BACKEND_LEXICAL

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


def build_shared_embedder(cfg: Any) -> Callable:
    """Return an embedder factory that loads nothing and caches one instance.

    ``ExpelAgent.__init__`` calls ``embedder(model_name=...)`` once per agent, and
    this design builds a fresh agent per task.  Both the download and the repeated
    construction are avoided: the backend is created lazily on first use and then
    shared, which is safe because embedding is stateless.

    Delegates the backend choice to :mod:`skillexpand.runtime.embedders`, which
    refuses to select any weight-downloading backend.
    """
    return embedders.build_from_config(cfg)


def build_agent(
    cfg: Any,
    task_idx: int = 0,
    rules: Optional[str] = None,
    no_rules: Optional[bool] = None,
    fewshot_strategy: Optional[str] = None,
    max_reflection_depth: Optional[int] = None,
    embedder_factory: Optional[Callable] = None,
    openai_api_key: Optional[str] = None,
    testing: bool = False,
    max_num_rules: Optional[int] = None,
    success_critique_num: Optional[int] = None,
    agent_cls: Optional[Any] = None,
):
    """Build one ExpeL agent, mirroring ``train.py:69-112``.

    Args:
        task_idx: which ``tasks`` entry the constructor should load first.  If
            you intend to call ``run(mode='eval', eval_idx=i)``, pass ``i`` so the
            constructor's throwaway environment matches the one you will use.
        rules: the skill body to inject directly, i.e. ``agent.rules``.  This is
            the *only* channel through which a Skill reaches the executor.
        no_rules: ``True`` reproduces the upstream Vanilla baseline.
        fewshot_strategy: override ``cfg.agent.fewshot_strategy``.  Pass
            ``FEWSHOT_NONE`` for any arm that must not see retrieved training
            trajectories.
        embedder_factory: from :func:`build_shared_embedder`; avoids reloading
            the embedding model per agent.
        testing: ``True`` makes the agent print prompts and ``input()`` for them
            interactively -- only ever for a deliberate manual dry run.

    Returns:
        An ``ExpelAgent`` with ``no_rules`` and ``rules`` set, in eval mode.
    """
    tasks = INIT_TASKS_FN[cfg.benchmark.name](cfg)
    if not 0 <= task_idx < len(tasks):
        raise IndexError(f'task_idx {task_idx} outside 0..{len(tasks) - 1}')

    # Credentials are resolved from the environment, exactly as train.py/eval.py do
    # (they read OPENAI_API_KEY or prompt).  A default placeholder is not safe: a
    # self-hosted vLLM ignores the key, but a hosted gateway answers
    # {"message":"access key invalid or expired"} with HTTP 401 -- and the failure
    # surfaces hours later as a mid-run crash rather than at construction.
    if openai_api_key is None:
        openai_api_key = os.environ.get('OPENAI_API_KEY') or ''
        if not openai_api_key:
            from skillexpand.runtime.models.llm import get_llm_base_url
            if get_llm_base_url():
                raise RuntimeError(
                    'no API key: OPENAI_API_KEY is unset but EXPE_LLM_BASE_URL '
                    'points at a hosted endpoint. Configure .env from '
                    '.env.example and source scripts/env.sh.')
            openai_api_key = 'EMPTY'

    strategy = fewshot_strategy or cfg.agent.fewshot_strategy
    depth = max_reflection_depth if max_reflection_depth is not None\
        else (cfg.agent.max_reflection_depth if 'max_reflection_depth' in cfg.agent.keys() else 0)

    cls = agent_cls or AGENT[cfg.agent_type]
    agent = cls(
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
        reflection_fewshots=REFLECTION_FEWSHOTS[cfg.benchmark.name],
        reflection_task_prompt=HUMAN_REFLECTION_INSTRUCTION[cfg.benchmark.name],
        reflection_system_instruction=SYSTEM_REFLECTION_INSTRUCTION[cfg.benchmark.name],
        reflection_system_prompt=SYSTEM_INSTRUCTION[cfg.benchmark.name],
        max_relfection_depth=depth,
        system_critique_instructions=SYSTEM_CRITIQUE_INSTRUCTION[cfg.benchmark.name],
        human_critiques=HUMAN_CRITIQUES,
        # Rule growth is intentionally unbounded.  The old 20-rule cap made
        # the executor silently change its add/remove policy once a Skill grew
        # past an arbitrary threshold; structured L2 edits already constrain
        # each individual change.
        max_num_rules=max_num_rules,
        rule_template=RULE_TEMPLATE[cfg.benchmark.name],
        truncate_strategy=cfg.agent.truncate_strategy if 'truncate_strategy' in cfg.agent.keys() else None,
        llm_parser=LLM_PARSER[cfg.benchmark.name],
        observation_formatter=OBSERVATION_FORMATTER[cfg.benchmark.name],
        embedder=embedder_factory or embedders.build_from_config(cfg),
        embedder_path=cfg.agent.retrieval_kwargs.embedder_path,
        step_stripper=STEP_STRIPPER[cfg.benchmark.name],
        retriever_cls=RETRIEVERS(cfg.agent.retrieval_kwargs.retriever_type),
        message_splitter=CYCLER[cfg.benchmark.name],
        identifier=STEP_IDENTIFIER[cfg.benchmark.name],
        message_step_splitter=partial(STEP_CYCLER, benchmark=cfg.benchmark.name),
        reflection_prefix=REFLECTION_PREFIX[cfg.benchmark.name],
        previous_trials_formatter=PREVIOUS_TRIALS_FORMATTER[cfg.benchmark.name],
        success_critique_num=success_critique_num if success_critique_num is not None
        else cfg.agent.success_critique_num,
        fewshot_strategy=strategy,
        critique_truncate_strategy=cfg.agent.critique_truncate_strategy,
        critique_summary_suffix=CRITIQUE_SUMMARY_SUFFIX,
        testing=testing,
        task_idx=task_idx,
        benchmark_name=cfg.benchmark.name,
        reranker=cfg.agent.retrieval_kwargs.reranker,
        buffer_retrieve_ratio=cfg.agent.retrieval_kwargs.buffer_retrieve_ratio,
        max_fewshot_tokens=get_fewshot_max_tokens(cfg.benchmark.name)
        if cfg.agent.retrieval_kwargs.max_fewshot_tokens == 'auto'
        else cfg.agent.retrieval_kwargs.max_fewshot_tokens,
    )

    # See the module docstring: both attributes are read by
    # insert_before_task_prompt but never initialised upstream.
    #
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
    # Resolve per-benchmark prompt overrides for selection without configuring
    # the execution environment (panels retain their original executor).
    from skillexpand.l1.adapters import resolve
    agent.l1_adapter = resolve(cfg)

    # The constructor ran with training=True, so rules were not injected into the
    # prompt it built.  Switching to eval mode makes the next reset() (inside
    # run(mode='eval')) inject them.
    agent.eval()
    return agent


def run_executor_once(
    cfg: Any,
    task_id: int,
    skill_body: Optional[str] = None,
    fewshot_strategy: str = FEWSHOT_NONE,
    embedder_factory: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Run one task with a brand-new agent and return its outcome.

    This is ``A_fresh(x, S; E = empty)``: a fresh executor instance, no source
    experience, and -- by default -- no trajectory retrieval.

    Returns a dict rather than a ``TaskOutcome`` so that this module stays free of
    schema coupling; the evaluator wraps it.
    """
    agent = build_agent(
        cfg,
        task_idx=task_id,
        rules=skill_body,
        fewshot_strategy=fewshot_strategy,
        embedder_factory=embedder_factory,
        max_reflection_depth=0,  # executor only: no reflection trials
    )
    agent.run(mode='eval', eval_idx=task_id)
    return {
        'task_id': task_id,
        'success': bool(agent.is_success()),
        'num_steps': int(agent.curr_step),
        'truncated': bool(agent.truncated),
        'reward': bool(agent.reward),
    }


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
    from skillexpand.l1.adapters import resolve
    key=os.environ.get('OPENAI_API_KEY','')
    if not key:
        raise RuntimeError('OPENAI_API_KEY is required')
    selected = model or (role_model(cfg, role) if role else cfg.agent.llm)
    llm=LLM_CLS(llm_name=selected,openai_api_key=key,long_ver=False)
    if usage_path:
        from skillexpand.persistence.usage import attach_usage
        attach_usage([llm],usage_path)
    tokenizer=tiktoken.get_encoding('cl100k_base')
    return SimpleNamespace(llm=llm,token_counter=lambda text:len(tokenizer.encode(text)),
                           benchmark_name=cfg.benchmark.name,l1_adapter=resolve(cfg))
