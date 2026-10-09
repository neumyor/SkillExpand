"""Progressive Skill-library execution: catalog -> select -> load -> run.

The executor sees only the catalog (``skill_id`` + ``description``) while a
Skill is being chosen; the chosen Skill's body is resolved in a second step, so
the library is never injected wholesale.  The worker here is the progressive
counterpart of ``l1.workers.execute_experience``: it shares the Harbor
payload-to-experience conversion with the fixed worker and differs only in how
the Skill and the card's selection identity are chosen.
"""
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from skillexpand import schema as S
from skillexpand.evaluation.selector import SkillSelector
from skillexpand.l1.workers import harbor_experience
from skillexpand.reliability.units import failure_record
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL

LOAD_STAGE = 'after_selection'


def catalog(skills: Iterable[S.Skill]) -> list[dict[str, str]]:
    """Return the selector-visible part of the library, without Skill bodies."""
    return [
        {'skill_id': skill.skill_id, 'description': skill.description}
        for skill in sorted(skills, key=lambda item: item.skill_id)
    ]


def select_and_load(host: Any, task_text: str,
                    skills: Iterable[S.Skill]) -> Tuple[S.Skill, Dict[str, Any]]:
    """Select one Skill from the catalog, then load exactly that Skill body."""
    library = list(skills)
    if not library:
        raise ValueError('progressive Skill library is empty')
    choice = SkillSelector(host).select(task_text, library)
    visible_catalog = catalog(library)
    # Keep the selector evidence self-contained, while making it impossible to
    # accidentally persist a Skill body in the selector-visible catalog.
    if any('body' in item or 'key' in item for item in visible_catalog):
        raise AssertionError('progressive selector catalog leaked Skill body')
    record = {'stage': 'select', **asdict(choice), 'catalog': visible_catalog,
              'catalog_fingerprint': S.content_hash(visible_catalog)}
    if not choice.ok:
        raise RuntimeError(f'progressive Skill selection failed: {choice.reason}')
    selected = next((skill for skill in library if skill.skill_id == choice.skill_id), None)
    if selected is None:
        raise RuntimeError(f'selector returned unknown Skill: {choice.skill_id}')
    record.update({
        'stage': 'load',
        'loaded_skill_id': selected.skill_id,
        'loaded_skill_key': selected.key,
        'loaded_body_chars': len(selected.body),
        'load_stage': LOAD_STAGE,
    })
    return selected, record


@dataclass(frozen=True)
class ProgressiveSpec:
    """One progressive L1 unit; the whole library travels as plain dicts."""

    unit_id: str
    benchmark: str
    task_id: int
    skill_library: Tuple[Dict[str, Any], ...]
    l1_checkpoint_path: str
    split: str = S.SPLIT_TRAIN
    max_trials: Optional[int] = None
    evolution_round: int = 0
    #: Selector usage ledger; defaults to ``<checkpoint stem>.selector.json``.
    usage_path: Optional[str] = None


def selector_usage_path(spec: ProgressiveSpec) -> str:
    return spec.usage_path or str(Path(spec.l1_checkpoint_path).with_suffix('.selector.json'))


def execute_progressive_experience(spec: ProgressiveSpec) -> Dict[str, Any]:
    """Select a Skill from the catalog, run it, and return a JSON-safe record.

    A failed unit is reported, never raised, so it can cross the process
    boundary; the stage's ``FailureCollector`` applies its disposition.
    """
    started = time.time()
    try:
        cfg = PL._config(spec.benchmark)
        rollout = cfg.benchmark.get('rollout', {})
        if spec.benchmark != 'terminalbench' or rollout.get('mode') != 'harbor_rollout':
            raise ValueError('progressive library execution requires terminalbench harbor_rollout')
        library = tuple(S.from_dict(S.Skill, item) for item in spec.skill_library)
        task = F.task_table(cfg)[spec.task_id]['task']
        host = F.build_reasoning_host(cfg, selector_usage_path(spec), role='selector')
        skill, selection = select_and_load(host, task, library)
        skill_load = {'skill_id': skill.skill_id, 'skill_key': skill.key,
                      'body_chars': len(skill.body), 'load_stage': LOAD_STAGE}
        record = harbor_experience(
            cfg, unit_id=spec.unit_id, task_id=spec.task_id, family_id=skill.family_id,
            split=spec.split, skill=skill, skill_key=skill.key,
            selected_skill_id=skill.skill_id, selection_source=S.SELECTION_AGENT,
            selection_reason=selection.get('why', ''), selection_raw=selection.get('raw', ''),
            max_trials=spec.max_trials, l1_checkpoint_path=spec.l1_checkpoint_path,
            evolution_round=spec.evolution_round)
        record.update({'selection': selection, 'skill_load': skill_load,
                       'secs': round(time.time() - started, 1)})
        return record
    except Exception as exc:  # noqa: BLE001 - the failure record carries the category
        return {
            'record_type': 'experience', 'unit_id': spec.unit_id, 'task_id': spec.task_id,
            'family_id': None, 'ok': False, 'experience': None,
            'selection': None, 'skill_load': None,
            'secs': round(time.time() - started, 1),
            'failure': failure_record(exc, unit_id=spec.task_id, stage='l1'),
            'pid': os.getpid(),
        }
