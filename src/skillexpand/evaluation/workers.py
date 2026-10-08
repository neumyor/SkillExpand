"""Process-pool entry point for single-attempt val/test execution of a fixed Skill."""
import time
from dataclasses import dataclass
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
