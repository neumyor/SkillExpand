
import multiprocessing
import os
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from skillexpand.runtime import agent_factory as F
from skillexpand.evaluation import evaluator as EV
from skillexpand import schema as S
from skillexpand.runtime.deadline import close_environment, worker_timeout

#: Hard ceiling offered by the backend.  Kept explicit so a typo cannot ask for
#: 640 workers.
MAX_WORKERS = 256

#: Per-process caches.  A spawned worker re-imports this module, so anything
#: expensive belongs here rather than in every unit.
_CFG_CACHE: Dict[str, Any] = {}
_EMBEDDER_CACHE: Dict[str, Any] = {}


def _config(benchmark: str):
    path=os.environ.get('EXPE_CONFIG_FILE','')
    key=(benchmark,path)
    if key not in _CFG_CACHE:
        from omegaconf import OmegaConf
        _CFG_CACHE[key] = OmegaConf.load(path) if path else F.load_config(benchmark)
    return _CFG_CACHE[key]


def _embedder(benchmark: str):
    if benchmark not in _EMBEDDER_CACHE:
        _EMBEDDER_CACHE[benchmark] = F.build_shared_embedder(_config(benchmark))
    return _EMBEDDER_CACHE[benchmark]


@dataclass(frozen=True)
class UnitSpec:
    """Everything a worker needs, as plain picklable data.

    ``skill_body`` travels as text rather than as a ``Skill`` object so that a
    worker never needs the skill library on disk; the parent is the single source
    of truth for what was injected, and it records ``skill_key`` alongside the
    outcome for traceability.
    """

    unit_id: str
    benchmark: str
    task_id: int
    role: str
    arm_id: str
    mode: str
    fewshot_strategy: str
    skill_key: Optional[str] = None
    skill_body: Optional[str] = None
    repeat: int = 0
    max_steps: Optional[int] = None
    capture_trajectory: bool = False
    skill_library: tuple = ()
    usage_path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.role not in S.ROLES:
            raise ValueError(f'unknown role {self.role!r}')
        if self.arm_id not in S.ARMS:
            raise ValueError(f'unknown arm_id {self.arm_id!r}')


def execute(spec: UnitSpec) -> Dict[str, Any]:
    """Run one unit in the calling process and return a JSON-safe record.

    Module-level and picklable on purpose: macOS defaults to the ``spawn`` start
    method, so a closure or bound method would not survive the trip to a worker.
    """
    cfg = _config(spec.benchmark)
    max_steps_default = int(cfg.benchmark.max_steps)
    max_steps = spec.max_steps if spec.max_steps is not None else max_steps_default

    started = time.time()
    error: Optional[str] = None
    success = terminated = truncated = False
    steps = 0
    family = 'unknown'
    trajectory: Optional[str] = None
    selected_key=spec.skill_key
    selection=None
    try:
        # Family resolution is inside the guard on purpose: it indexes the task
        # list, so an out-of-range task id raises here, and anything raised outside
        # the guard would escape the worker and take the whole pool down instead of
        # being recorded as one failed unit.
        family = 'heldout'
        if spec.skill_library:
            return execute_routed(spec,cfg)
        raise ValueError('Held-out evaluation requires a complete Skill library')
    except Exception as exc:  # noqa: BLE001 - a crashed unit is still a unit
        error = f'{type(exc).__name__}: {exc}'

    failure_mode = EV.classify_failure(success, terminated, truncated, steps,
                                       max_steps, error)
    return {
        'record_type': 'unit',
        'unit_id': spec.unit_id,
        'task_id': spec.task_id,
        'family': family,
        'role': spec.role,
        'arm_id': spec.arm_id,
        'mode': spec.mode,
        'repeat': spec.repeat,
        'skill_key': spec.skill_key,
        'success': success,
        'steps': steps,
        'terminated': terminated,
        'truncated': truncated,
        'failure_mode': failure_mode,
        'secs': round(time.time() - started, 1),
        'error': error,
        'pid': os.getpid(),
        #: Kept out of the unit log by the callers' sinks: it is evidence for the
        #: editor, not a measurement, and it would dominate the file.
        'trajectory': trajectory,
    }


@dataclass(frozen=True)
class ExperienceSpec:
    """One layer-1 unit: gather task experience in a worker process.

    Gathering is the expensive half of an episode -- ExpeL's adaptation loop runs
    up to ``max_reflection_depth + 1`` episodes per task -- so it is worth running
    concurrently for the same reason evaluation is.  It is also safe to: the loop
    touches nothing but its own agent and environment, and the skill it injects
    travels as text.
    """

    unit_id: str
    benchmark: str
    task_id: int
    family_id: str
    split: str
    skill_key: Optional[str] = None
    skill_body: Optional[str] = None
    skill_description: str = ''
    meta_skill_version: Optional[int] = None
    fewshot_strategy: str = 'none'
    skill_aware: bool = True
    selected_skill_id: Optional[str] = None
    selection_source: str = S.SELECTION_FIXED
    selection_reason: str = ''
    selection_raw: str = ''
    skill_library: tuple = ()
    progressive_selection: bool = False
    frozen_selector_result: Optional[str] = None
    usage_path: Optional[str] = None
    #: Repair-loop trial cap: the first attempt plus one per allowed reflection.
    #: Carried explicitly so the loop's configured value reaches the worker instead of
    #: the worker silently falling back to the config file's reflection depth.
    max_trials: Optional[int] = None
    l1_checkpoint_path: Optional[str] = None
    supervised_repair: bool = True
    supervised_attempts: int = 1
    evolution_round: int = 0


def _reconstruct_skill(spec: 'ExperienceSpec') -> Optional[S.Skill]:
    """Rebuild just enough of a Skill for the gatherer, which uses key and body."""
    if not spec.skill_body:
        return None
    version = 0
    if spec.skill_key and '@v' in spec.skill_key:
        tail = spec.skill_key.rsplit('@v', 1)[1]
        version = int(tail) if tail.isdigit() else 0
    return S.Skill(
        skill_id=spec.selected_skill_id or (spec.skill_key.rsplit('@v', 1)[0] if spec.skill_key else f'{spec.benchmark}.{spec.family_id}'),
        family_id=(spec.selected_skill_id.partition('.')[2] if spec.selected_skill_id else spec.family_id),
        version=version,
        name=spec.family_id,
        description=spec.skill_description,
        body=spec.skill_body,
        provenance=S.Provenance(rationale='reconstructed in worker'))


def execute_experience(spec: ExperienceSpec) -> Dict[str, Any]:
    """Run ExpeL's adaptation loop for one task and return a JSON-safe record.

    A failed gather is reported, not raised: at a ~67% solve rate some tasks never
    converge, and a task that produced no usable experience is a datum about the
    executor, not a reason to lose the batch.
    """
    from skillexpand.l1 import experience as X

    started = time.time()
    try:
        cfg = _config(spec.benchmark)
        if spec.benchmark == 'terminalbench' and cfg.benchmark.get('rollout', {}).get('mode') == 'harbor_rollout':
            from skillexpand.benchmarks.terminalbench import harbor_rollout
            from skillexpand.l1 import protocol as P
            from skillexpand.l1 import learning as L
            selection = None
            selection_catalog = ()
            skill_load = None
            if spec.progressive_selection:
                from skillexpand.runtime.progressive import select_and_load, catalog
                library = tuple(S.from_dict(S.Skill, item) for item in spec.skill_library)
                task = F.task_table(cfg)[spec.task_id]['task']
                if spec.frozen_selector_result:
                    cached = json.loads(Path(spec.frozen_selector_result).read_text())
                    previous = cached['experience']
                    choice = cached['selection']
                    if (not cached.get('ok') or previous['task_id'] != spec.task_id or
                            previous['task'] != task or choice.get('catalog') != catalog(library) or
                            not choice.get('ok')):
                        raise ValueError('Frozen selector task/catalog identity mismatch')
                    class CachedSelectorHost:
                        def llm(self, *args, **kwargs):
                            return choice['raw']
                    host = CachedSelectorHost()
                else:
                    host = F.build_reasoning_host(
                        cfg, spec.usage_path or str(Path(spec.l1_checkpoint_path).with_suffix('.selector.json')),
                        role='selector')
                skill, selection = select_and_load(host, task, library)
                if spec.frozen_selector_result:
                    if selection['skill_id'] != choice['skill_id']:
                        raise ValueError('Frozen selector raw response disagrees with selected Skill')
                    selection['reused_from_result'] = spec.frozen_selector_result
                selection_catalog = tuple(selection.get('catalog', ()))
                skill_load = {
                    'skill_id': skill.skill_id,
                    'skill_key': skill.key,
                    'body_chars': len(skill.body),
                    'load_stage': 'after_selection',
                }
                family_id = skill.family_id
                skill_key = skill.key
                selected_skill_id = skill.skill_id
                selection_source = S.SELECTION_AGENT
                selection_reason = selection.get('why', '')
                selection_raw = selection.get('raw', '')
            else:
                skill = _reconstruct_skill(spec)
                family_id = spec.family_id
                skill_key = spec.skill_key
                selected_skill_id = spec.selected_skill_id
                selection_source = spec.selection_source
                selection_reason = spec.selection_reason
                selection_raw = spec.selection_raw
                selection_catalog = tuple(spec.skill_library)
                skill_load = ({'skill_id': skill.skill_id, 'skill_key': skill.key,
                               'body_chars': len(skill.body), 'load_stage': 'fixed'}
                              if skill else None)
            task = F.task_table(cfg)[spec.task_id]['task']
            payload, raw_response = harbor_rollout(
                cfg, spec.task_id, skill, spec.max_trials or 1,
                Path(spec.l1_checkpoint_path).parent.parent / 'harbor',
                spec.evolution_round)
            trials = []
            for item in payload.get('trials', []):
                trajectory = item.get('trajectory_path') or item.get('trial_dir')
                events = []
                trajectory_file = Path(trajectory) if trajectory else None
                if trajectory_file and trajectory_file.is_file():
                    try:
                        trace = json.loads(trajectory_file.read_text())
                        for index, step in enumerate(trace.get('steps', []), 1):
                            text = step.get('message') or step.get('observation') or ''
                            if text:
                                events.append({'ref': f'e{index}', 'action': 'TerminalBatch',
                                               'observation': str(text),
                                               'environment': {'success': item.get('reward') == 1}})
                    except (OSError, ValueError, TypeError):
                        pass
                reward = item.get('reward') == 1
                trials.append({'index': int(item.get('attempt_index', len(trials) + 1)),
                               'phase': 'autonomous', 'status': 'completed', 'success': reward,
                               'termination': item.get('status', 'verifier'),
                               'trajectory': trajectory, 'events': events or [{'ref': 'e1',
                               'action': 'TerminalBatch', 'observation': 'No trajectory steps recorded.',
                               'environment': {'success': reward}}]})
            rewards = tuple(bool(t['success']) for t in trials)
            solved = any(rewards)
            evidence = P.evidence(task, trials, adapter=__import__('skillexpand.l1.adapters', fromlist=['resolve']).resolve(cfg))
            card = L.card(spec.task_id, task, trials, {'status': 'valid', 'claims': []},
                          'terminalbench_harbor', f'{spec.unit_id}:card', len,
                          evidence=evidence, card_id=f'{spec.unit_id}:card',
                          benchmark='terminalbench', family_id=family_id,
                          evolution_round=spec.evolution_round, skill_key=skill_key)
            exp = S.TaskExperience(
                experience_id=f'{spec.unit_id}:experience', benchmark='terminalbench',
                task_id=spec.task_id, task=task, family_id=family_id, split=spec.split,
                reward=solved, num_trials=len(trials), initial_skill_key=skill_key,
                failed_trajectories=tuple(t['trajectory'] for t in trials if not t['success']),
                final_trajectory=next((t['trajectory'] for t in reversed(trials) if t['success']), None),
                selected_skill_id=selected_skill_id, selection_source=selection_source,
                selection_reason=selection_reason, selection_raw=selection_raw,
                selection_catalog=selection_catalog, skill_load=skill_load,
                trial_rewards=rewards, trial_phases=tuple(t['phase'] for t in trials),
                experience_card=card, l1_audit_path=spec.l1_checkpoint_path,
                l1_trials=tuple(trials), evolution_round=spec.evolution_round)
            return {'record_type': 'experience', 'unit_id': spec.unit_id, 'task_id': spec.task_id,
                    'family_id': family_id, 'selection': selection, 'ok': True, 'experience': S.to_dict(exp),
                    'harbor_response': raw_response, 'secs': round(time.time() - started, 1),
                    'error': None, 'pid': os.getpid()}
        experience, agent = X.gather_task_experience(
            cfg, spec.task_id, spec.family_id, spec.split,
            skill=_reconstruct_skill(spec),
            meta_skill_version=spec.meta_skill_version,
            embedder_factory=_embedder(spec.benchmark),
            skill_aware=spec.skill_aware,
            selected_skill_id=spec.selected_skill_id,
            selection_source=spec.selection_source,
            selection_reason=spec.selection_reason,
            selection_raw=spec.selection_raw,
            max_attempts=spec.max_trials, checkpoint_path=spec.l1_checkpoint_path,
            supervised_repair=spec.supervised_repair,
            supervised_attempts=spec.supervised_attempts,
            evolution_round=spec.evolution_round)
        if agent is not None:
            close_environment(agent)
        return {
            'record_type': 'experience',
            'unit_id': spec.unit_id,
            'task_id': spec.task_id,
            'family_id': spec.family_id,
            'ok': True,
            'experience': S.to_dict(experience),
            'secs': round(time.time() - started, 1),
            'error': None,
            'pid': os.getpid(),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            'record_type': 'experience',
            'unit_id': spec.unit_id,
            'task_id': spec.task_id,
            'family_id': spec.family_id,
            'ok': False,
            'experience': None,
            'secs': round(time.time() - started, 1),
            'error': f'{type(exc).__name__}: {exc}',
            'pid': os.getpid(),
        }


def effective_workers(requested: int, n_units: int) -> int:
    """Clamp a worker count to something sane for the batch."""
    if requested <= 0:
        return 1
    return max(1, min(int(requested), MAX_WORKERS, max(1, n_units)))


def run_generic(specs: Sequence[Any],
                worker: Callable[[Any], Dict[str, Any]],
                workers: int = 1,
                on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
                chunksize: int = 1,
                verbose: bool = False) -> List[Dict[str, Any]]:
    """Execute specs with an arbitrary module-level worker.

    ``worker`` must be a module-level function: macOS defaults to the ``spawn``
    start method, so closures and bound methods cannot cross the process boundary.

    Results are persisted as they finish; a slow first task cannot hold completed
    units in memory. Callers identify records by task ID, never arrival order.
    """
    specs = list(specs)
    n = effective_workers(workers, len(specs))
    results: List[Dict[str, Any]] = []

    native = any(getattr(spec, 'benchmark', None) == 'alfworld' and
                 getattr(spec, 'requires_native_environment', True) for spec in specs)
    if n <= 1 and not native:
        for spec in specs:
            record = worker(spec)
            results.append(record)
            if on_result:
                on_result(record)
            if verbose:
                _log(record)
        return results

    # Routing never constructs a native environment. It and SearchQA's private
    # in-memory indexes can share imports without sharing per-task state.
    if all(getattr(spec, 'benchmark', None) == 'searchqa' or
           not getattr(spec, 'requires_native_environment', True) for spec in specs):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        for benchmark in {spec.benchmark for spec in specs}:
            _config(benchmark)
            if benchmark == 'searchqa':
                _embedder(benchmark)
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix='benchmark-task') as executor:
            futures = [executor.submit(worker, spec) for spec in specs]
            for future in as_completed(futures):
                record = future.result()
                results.append(record)
                if on_result:
                    on_result(record)
                if verbose:
                    _log(record)
        return results

    ctx = multiprocessing.get_context('spawn')
    timeout = worker_timeout()
    with ctx.Pool(processes=n, maxtasksperchild=1) as pool:
        # One unit per child allows reliable cleanup after native failures. A
        # progress deadline also catches crashed workers and uninterruptible C
        # calls that Python's environment alarm cannot interrupt.
        iterator = pool.imap_unordered(worker, specs, chunksize=1)
        for _ in specs:
            try:
                record = iterator.next(timeout=timeout)
            except multiprocessing.TimeoutError as exc:
                raise TimeoutError(f'No worker completed within {timeout:g}s; resume pending units') from exc
            results.append(record)
            if on_result:
                on_result(record)
            if verbose:
                _log(record)
    return results


def run(specs: Sequence[UnitSpec], workers: int = 1,
        on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
        chunksize: int = 1,
        verbose: bool = False) -> List[Dict[str, Any]]:
    """Execute evaluation specs, optionally across processes.

    ALFWorld always uses isolated processes, including single-worker runs.
    """
    return run_generic(specs, execute, workers=workers, on_result=on_result,
                       chunksize=chunksize, verbose=verbose)


def _log(record: Dict[str, Any]) -> None:
    print(f'  task {record["task_id"]:3d} r{record["repeat"]} '
          f'{record["arm_id"]:9s}/{record["role"]:7s} '
          f'success={str(record["success"]):5s} '
          f'steps={record["steps"]:2d} {record["secs"]:6.1f}s '
          f'[{record["failure_mode"] or "solved"}]', flush=True)


def execute_routed(spec,cfg):
    """A fresh environment using catalog -> select -> load progressive routing."""
    from skillexpand.l1.agent import RepairAgent
    from skillexpand.l1.adapters import resolve
    from skillexpand.runtime.progressive import select_and_load
    started=time.time()
    library=[S.from_dict(S.Skill,s) for s in spec.skill_library]
    agent=F.build_agent(cfg,task_idx=spec.task_id,rules=None,fewshot_strategy='none',
                        agent_cls=RepairAgent,max_reflection_depth=0)
    if spec.usage_path:
        from skillexpand.persistence.usage import attach_usage
        attach_usage([agent.llm,agent.long_context_llm],spec.usage_path)
    try:
        skill, selection_record = select_and_load(
            agent, F.task_text_of(cfg, spec.task_id), library)
    except Exception as exc:
        selection_record = {"stage": "select", "error": f"{type(exc).__name__}: {exc}"}
        choice = None
    else:
        choice = selection_record
    base=dict(record_type='unit',unit_id=spec.unit_id,task_id=spec.task_id,family='heldout',
        role=spec.role,arm_id=spec.arm_id,mode=spec.mode,repeat=spec.repeat,
        selection=selection_record,skill_key=None,success=False,steps=0,terminated=False,
        skill_load=None,
        truncated=False,failure_mode='routing_failure',error=None,trajectory=None,pid=os.getpid())
    if choice is None:
        base['error'] = selection_record.get('error', 'selector provider error')
        base['secs']=round(time.time()-started,2)
        return base
    base['skill_load'] = {
        'skill_id': skill.skill_id, 'skill_key': skill.key,
        'body_chars': len(skill.body), 'mode': 'progressive'
    }
    agent.rules,agent.no_rules=skill.body,not bool(skill.body)
    adapter=resolve(cfg);adapter.configure(agent)
    result=agent.execute_trial(adapter,{},'')
    base.update(skill_key=skill.key,success=result['success'],
        steps=sum('action' in e for e in result['events']),terminated=bool(agent.terminated),
        truncated=bool(agent.truncated),failure_mode=None if result['success'] else result['termination'],
        secs=round(time.time()-started,2))
    return base


def execute_fixed(spec):
    """Execute the preselected Skill once; no routing, reflection or guidance."""
    from skillexpand.l1.agent import RepairAgent
    from skillexpand.l1.adapters import resolve
    started = time.time()
    base = dict(task_id=spec.task_id, skill_key=spec.skill_key, success=False,
                steps=0, truncated=False, error=None, trajectory=None, events=[])
    agent = None
    try:
        cfg = _config(spec.benchmark)
        agent = F.build_agent(cfg, task_idx=spec.task_id, rules=spec.skill_body,
                              fewshot_strategy='none', agent_cls=RepairAgent,
                              max_reflection_depth=0)
        if spec.usage_path:
            from skillexpand.persistence.usage import attach_usage
            attach_usage([agent.llm, agent.long_context_llm], spec.usage_path)
        adapter = resolve(cfg)
        adapter.configure(agent)
        def capture(events):
            # Keep task-local evidence even when a later provider call fails.
            base['events'] = events
            base['steps'] = sum('action' in e for e in events)
            base['trajectory'] = '\n'.join(
                e.get('model_text', '') + ('\nObservation: ' + e['observation']
                                          if 'observation' in e else '') for e in events)

        result = agent.execute_trial(adapter, {}, '', on_event=capture)
        base.update(success=result['success'], steps=sum('action' in e for e in result['events']),
                    truncated=bool(agent.truncated),
                    failure_mode=None if result['success'] else result['termination'],
                    trajectory=result['trajectory'], events=result['events'])
    except Exception as exc:
        base.update(error=f'{type(exc).__name__}: {exc}', failure_mode='execution_error')
    finally:
        if agent is not None and hasattr(agent, 'env'):
            close_environment(agent)
    base['secs'] = round(time.time() - started, 2)
    return base
