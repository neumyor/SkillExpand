"""Bounded L1 task repair and append-only experience-card persistence."""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


from skillexpand import schema as S
from skillexpand.runtime import agent_factory as F

#: Number of attempts per task: the first trial plus one per allowed reflection.
#: Kept aligned with the benchmark config's ``agent.max_reflection_depth``.
DEFAULT_MAX_ATTEMPTS = 4


def gather_task_experience(cfg: Any, task_id: int, family_id: str, split: str,
                           skill: Optional[S.Skill] = None,
                           meta_skill_version: Optional[int] = None,
                           embedder_factory: Optional[Callable] = None,
                           skill_aware: bool = True,
                           max_attempts: Optional[int] = None,
                           verbose: bool = False,
                           selected_skill_id: Optional[str] = None,
                           selection_source: str = S.SELECTION_FIXED,
                           selection_reason: str = '',
                           selection_raw: str = '', checkpoint_path=None,
                           supervised_repair: bool = True,
                           supervised_attempts: int = 1,
                           evolution_round: int = 0) -> Tuple[S.TaskExperience, Any]:
    """Run L1 autonomous repair plus optional benchmark guidance; return its card and agent."""
    from skillexpand.l1.agent import RepairAgent
    from skillexpand.l1.runner import run
    agent = F.build_agent(cfg, task_idx=task_id,
                          rules=skill.body if skill and skill_aware else None,
                          fewshot_strategy=F.FEWSHOT_NONE,
                          embedder_factory=embedder_factory, agent_cls=RepairAgent)
    agent.train()
    try:
        return run(agent, cfg, task_id, family_id, split, skill, meta_skill_version,
                   selected_skill_id, selection_source, selection_reason, selection_raw,
                   k=max_attempts if max_attempts is not None else DEFAULT_MAX_ATTEMPTS,
                   supervised=supervised_repair, supervised_attempts=supervised_attempts,
                   checkpoint_path=checkpoint_path,
                   evolution_round=evolution_round)
    except BaseException:
        from skillexpand.runtime.deadline import close_environment
        close_environment(agent)
        raise


class ExperienceLog:
    """Append-only store of gathered experiences, keyed by experience id.

    Gathering is expensive -- up to four episodes per source task -- so a run that
    dies halfway must resume rather than restart.  Records are also the raw
    evidence behind every attribution, which means a re-analysis never needs the
    model again.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._by_id: Dict[str, S.TaskExperience] = {}
        self._replay()

    def _replay(self) -> None:
        if not self.path.exists():
            return
        from skillexpand.persistence.store import read_jsonl
        for rec in read_jsonl(self.path):
            exp = S.from_dict(S.TaskExperience, rec)
            if exp.experience_id in self._by_id:
                raise ValueError('Duplicate experience in persisted log')
            self._by_id[exp.experience_id] = exp

    def append(self, experience: S.TaskExperience) -> None:
        if experience.experience_id in self._by_id:
            raise ValueError('Duplicate experience')
        with self.path.open('a') as fh:
            fh.write(S.to_jsonl(experience) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        self._by_id[experience.experience_id] = experience

    def has(self, experience_id: str) -> bool:
        return experience_id in self._by_id

    def get(self, experience_id: str) -> S.TaskExperience:
        return self._by_id[experience_id]

    def all(self) -> List[S.TaskExperience]:
        return list(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def for_family(self, family_id: str) -> List[S.TaskExperience]:
        return sorted((e for e in self._by_id.values() if e.family_id == family_id),
                      key=lambda e: e.task_id)

    def for_split(self, split: str) -> List[S.TaskExperience]:
        return sorted((e for e in self._by_id.values() if e.split == split),
                      key=lambda e: e.task_id)
