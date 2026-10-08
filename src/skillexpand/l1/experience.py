"""Public L1 entry point: build the executor for one train task and run repair."""
from typing import Any, Optional, Tuple

from skillexpand import schema as S
from skillexpand.runtime import agent_factory as F

#: Autonomous attempts per task when the caller does not pass a budget.
DEFAULT_MAX_ATTEMPTS = 4


def gather_task_experience(cfg: Any, task_id: int, family_id: str, split: str,
                           skill: Optional[S.Skill] = None,
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
                          agent_cls=RepairAgent)
    try:
        return run(agent, cfg, task_id, family_id, split, skill,
                   selected_skill_id, selection_source, selection_reason, selection_raw,
                   k=max_attempts if max_attempts is not None else DEFAULT_MAX_ATTEMPTS,
                   supervised=supervised_repair, supervised_attempts=supervised_attempts,
                   checkpoint_path=checkpoint_path,
                   evolution_round=evolution_round)
    except BaseException:
        from skillexpand.runtime.deadline import close_environment
        close_environment(agent)
        raise
