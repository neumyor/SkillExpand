"""Process-pool entry point for one train-task L1 unit."""
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from skillexpand import schema as S
from skillexpand.reliability.units import failure_record
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand.runtime.deadline import close_environment


@dataclass(frozen=True)
class ExperienceSpec:
    """One L1 unit: autonomous repair plus optional supervision for one train task.

    The unit touches nothing but its own agent and environment, and the Skill it
    injects travels as text, so units run concurrently.
    """

    unit_id: str
    benchmark: str
    task_id: int
    family_id: str
    split: str
    skill_key: Optional[str] = None
    skill_body: Optional[str] = None
    skill_description: str = ''
    skill_aware: bool = True
    selected_skill_id: Optional[str] = None
    selection_source: str = S.SELECTION_FIXED
    selection_reason: str = ''
    selection_raw: str = ''
    #: Repair-loop trial cap: the first attempt plus one per allowed reflection.
    #: Carried explicitly so the loop's configured value reaches the worker instead of
    #: the worker silently falling back to the config file's reflection depth.
    max_trials: Optional[int] = None
    l1_checkpoint_path: Optional[str] = None
    supervised_repair: bool = True
    supervised_attempts: int = 1
    evolution_round: int = 0


def _reconstruct_skill(spec: ExperienceSpec) -> Optional[S.Skill]:
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


def _harbor_rollout(spec: ExperienceSpec, cfg) -> Dict[str, Any]:
    """Run one TerminalBench task through the external Harbor/Tencent runner.

    TerminalBench environments are not executable in-process: the real rollout
    happens inside the remote task sandbox, and this worker consumes its saved
    trajectory and verifier result to build the experience card.
    """
    from skillexpand.benchmarks.terminalbench import harbor_rollout
    from skillexpand.l1 import learning as L
    from skillexpand.l1 import protocol as P
    from skillexpand.l1.adapters import resolve

    skill = _reconstruct_skill(spec)
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
                trace = json.loads(trajectory_file.read_text(encoding='utf-8'))
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
                       'trajectory': trajectory,
                       'events': events or [{'ref': 'e1', 'action': 'TerminalBatch',
                                             'observation': 'No trajectory steps recorded.',
                                             'environment': {'success': reward}}]})
    rewards = tuple(bool(t['success']) for t in trials)
    card = L.card(spec.task_id, task, trials, {'status': 'valid', 'claims': []},
                  None, f'{spec.unit_id}:card', len,
                  evidence=P.evidence(task, trials, adapter=resolve(cfg)),
                  card_id=f'{spec.unit_id}:card', benchmark='terminalbench',
                  family_id=spec.family_id, evolution_round=spec.evolution_round,
                  skill_key=spec.skill_key)
    experience = S.TaskExperience(
        experience_id=f'{spec.unit_id}:experience', benchmark='terminalbench',
        task_id=spec.task_id, task=task, family_id=spec.family_id, split=spec.split,
        reward=any(rewards), num_trials=len(trials), initial_skill_key=spec.skill_key,
        failed_trajectories=tuple(t['trajectory'] for t in trials if not t['success']),
        final_trajectory=next((t['trajectory'] for t in reversed(trials) if t['success']), None),
        selected_skill_id=spec.selected_skill_id, selection_source=spec.selection_source,
        selection_reason=spec.selection_reason, selection_raw=spec.selection_raw,
        trial_rewards=rewards, trial_phases=tuple(t['phase'] for t in trials),
        experience_card=card, l1_audit_path=spec.l1_checkpoint_path,
        l1_trials=tuple(trials), evolution_round=spec.evolution_round)
    return {'record_type': 'experience', 'unit_id': spec.unit_id,
            'task_id': spec.task_id, 'family_id': spec.family_id, 'ok': True,
            'experience': S.to_dict(experience), 'harbor_response': raw_response,
            'failure': None, 'pid': os.getpid()}


def execute_experience(spec: ExperienceSpec) -> Dict[str, Any]:
    """Run the L1 repair loop for one task and return a JSON-safe record.

    A failed unit is reported, never raised, so it can cross the process
    boundary; the stage's ``FailureCollector`` applies its disposition.
    """
    from skillexpand.l1 import experience as X

    started = time.time()
    try:
        cfg = PL._config(spec.benchmark)
        rollout = cfg.benchmark.get('rollout', {})
        if spec.benchmark == 'terminalbench' and rollout.get('mode') == 'harbor_rollout':
            record = _harbor_rollout(spec, cfg)
            record['secs'] = round(time.time() - started, 1)
            return record
        experience, agent = X.gather_task_experience(
            cfg, spec.task_id, spec.family_id, spec.split,
            skill=_reconstruct_skill(spec),
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
            'failure': None,
            'pid': os.getpid(),
        }
    except Exception as exc:  # noqa: BLE001 - the failure record carries the category
        failure = failure_record(exc, unit_id=spec.task_id, stage='l1')
        return {
            'record_type': 'experience',
            'unit_id': spec.unit_id,
            'task_id': spec.task_id,
            'family_id': spec.family_id,
            'ok': False,
            'experience': None,
            'secs': round(time.time() - started, 1),
            'failure': failure,
            'pid': os.getpid(),
        }
