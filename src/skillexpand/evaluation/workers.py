"""Process-pool entry point for single-attempt val/test execution of a fixed Skill."""
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from skillexpand.reliability.units import failure_record
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand.runtime.deadline import close_environment


@dataclass(frozen=True)
class FixedSpec:
    """Plain picklable data for one execution; the Skill travels as text.

    ``skill_key`` is recorded with the outcome for traceability; the worker
    never reads the Skill library.
    """

    unit_id: str
    benchmark: str
    task_id: int
    skill_key: Optional[str] = None
    skill_body: Optional[str] = None
    usage_path: Optional[str] = None


def _harbor_fixed(spec: FixedSpec, cfg, started: float) -> Dict[str, Any]:
    """One TerminalBench val/test execution through the external Harbor runner.

    Same contract as the in-process path: one attempt, no reflection, and the
    environment's verifier result decides success.  The rollout artifacts are
    kept next to the score cache so the sampled verifier can read the cached
    trajectories back instead of rerunning anything.
    """
    from skillexpand.benchmarks.terminalbench import harbor_rollout

    out_dir = (Path(spec.usage_path).parent.parent / 'harbor' if spec.usage_path
               else Path(tempfile.mkdtemp(prefix='skillexpand-harbor-')))
    payload, raw = harbor_rollout(
        cfg, spec.task_id, SimpleNamespace(body=spec.skill_body or ''), 1, out_dir)
    trials = payload.get('trials') or []
    if not trials:
        raise RuntimeError('TerminalBench rollout returned no trial')
    trial = trials[0]
    events = []
    trajectory_file = Path(trial['trajectory_path']) if trial.get('trajectory_path') else None
    if trajectory_file and trajectory_file.is_file():
        try:
            trace = json.loads(trajectory_file.read_text(encoding='utf-8'))
            for index, step in enumerate(trace.get('steps', []), 1):
                message = str(step.get('message') or '')
                observation = str(step.get('observation') or '')
                if message or observation:
                    events.append({'ref': f'e{index}', 'model_text': message,
                                   'action': 'TerminalBatch', 'observation': observation})
        except (OSError, ValueError, TypeError):
            events = []
    success = trial.get('reward') == 1
    return {
        'task_id': spec.task_id, 'skill_key': spec.skill_key, 'success': success,
        'steps': len(events), 'truncated': False,
        'failure_mode': None if success else (
            trial.get('exception_type') or 'verifier_rejected'),
        'trajectory': trial.get('trajectory_path') or chr(10).join(
            event['observation'] for event in events),
        'events': events, 'harbor_run_id': raw.get('run_id'),
        'secs': round(time.time() - started, 2),
    }


def execute_fixed(spec: FixedSpec) -> Dict[str, Any]:
    """Execute the preselected Skill once; no routing, reflection or guidance."""
    from skillexpand.l1.agent import RepairAgent
    from skillexpand.l1.adapters import resolve
    started = time.time()
    base = dict(task_id=spec.task_id, skill_key=spec.skill_key, success=False,
                steps=0, truncated=False, failure=None, trajectory=None, events=[])
    agent = None
    try:
        cfg = PL._config(spec.benchmark)
        if (spec.benchmark == 'terminalbench'
                and cfg.benchmark.get('rollout', {}).get('mode') == 'harbor_rollout'):
            return _harbor_fixed(spec, cfg, started)
        agent = F.build_agent(cfg, task_idx=spec.task_id, rules=spec.skill_body,
                              agent_cls=RepairAgent)
        if spec.usage_path:
            from skillexpand.persistence.usage import attach_usage
            attach_usage([agent.llm], spec.usage_path)
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
    except Exception as exc:  # noqa: BLE001 - the failure record carries the category
        failure = failure_record(exc, unit_id=spec.unit_id, stage='fixed-execution')
        base.update(failure_mode='execution_error', failure=failure)
    finally:
        if agent is not None and hasattr(agent, 'env'):
            close_environment(agent)
    base['secs'] = round(time.time() - started, 2)
    return base
