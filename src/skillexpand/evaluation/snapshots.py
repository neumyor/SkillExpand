"""Single-attempt test evaluation of one frozen Skill library on frozen routes.

The CLI test phase and the per-round snapshot tool share this implementation.
The directory layout is the one :func:`skillexpand.evaluation.audit.audit_test`
recomputes: ``library.json``, ``protocol.json``, ``score_protocol.json``,
``scores.jsonl``, ``skills/<skill_id>.json``, ``summary.json`` and ``audit.json``.
"""
from omegaconf import OmegaConf

from skillexpand import schema as S
from skillexpand.evaluation.audit import audit_test
from skillexpand.evaluation.validation import FixedSkillScorer, ScoreCache, library_fingerprint
from skillexpand.persistence.io import freeze, save
from skillexpand.reliability.errors import StageIncomplete


def freeze_protocol(cfg, skills, initial, target, protocol_extra=None):
    """Freeze the library and execution protocol before any test task is touched."""
    freeze(target / 'library.json', [S.to_dict(s) for s in skills])
    freeze(target / 'protocol.json', {
        'config': OmegaConf.to_container(cfg, resolve=True),
        'routing_reference': [S.to_dict(s) for s in initial],
        **(protocol_extra or {}),
    })


def evaluate_library(cfg, plan, run_root, skills, initial, routes, target, workers,
                     protocol_extra=None, summary_extra=None, smoke=False):
    """Freeze, execute and audit ``skills`` once per routed test task.

    Routing uses the frozen initial descriptions, so every library of a run is
    measured on the same task-to-family assignment.  A route group that fails
    does not stop the others; its missing units stay uncached, the evaluation
    raises afterwards, and a resumed call executes only those units.
    ``smoke`` measures one task per group and skips the full-coverage audit.
    """
    skills = tuple(skills)
    freeze_protocol(cfg, skills, initial, target, protocol_extra)
    scorer = FixedSkillScorer(cfg, ScoreCache(target / 'scores.jsonl'), routes, workers)
    freeze(target / 'score_protocol.json', {'hash': scorer.protocol_hash})
    per_skill, failed = {}, []
    for skill in skills:
        task_ids = routes.groups[skill.skill_id]
        if smoke:
            task_ids = task_ids[:1]
        try:
            result = scorer.score(skill, task_ids, f'test:{routes.fingerprint}:{skill.skill_id}')
        except StageIncomplete as exc:
            # Only retryable group failures continue; anything that halts propagates.
            failed.append({'skill_id': skill.skill_id, 'error': str(exc), 'failures': exc.failures})
        else:
            per_skill[skill.skill_id] = {'tasks': result.n, 'successes': result.successes,
                                         'score': result.score}
            save(target / 'skills' / f'{skill.skill_id}.json', per_skill[skill.skill_id])
        save(target / 'progress.json', {'status': 'incomplete', 'completed_skills': per_skill,
                                        'failed_skills': failed})
    if failed:
        raise StageIncomplete(
            f'Incomplete Skill evaluation; failed groups: {[f["skill_id"] for f in failed]}',
            [unit for group in failed for unit in group['failures']])
    if smoke:
        tasks = sum(item['tasks'] for item in per_skill.values())
    else:
        tasks = len(plan.tasks_in(S.SPLIT_TEST))
    successes = sum(item['successes'] for item in per_skill.values())
    summary = {
        'status': 'complete',
        'split': 'test',
        'library_hash': library_fingerprint(skills),
        'routing_reference': 'initial_skills',
        'tasks': tasks,
        'successes': successes,
        'score': successes / tasks if tasks else None,
        'per_skill': per_skill,
        'routing_failures': list(routes.failed_task_ids),
        **(summary_extra or {}),
    }
    save(target / 'summary.json', summary)
    save(target / 'progress.json', {'status': 'complete', 'completed_skills': per_skill,
                                    'failed_skills': []})
    if not smoke:
        save(target / 'audit.json', audit_test(run_root, target))
    return summary
