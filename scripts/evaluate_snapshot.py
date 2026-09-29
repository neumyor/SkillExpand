#!/usr/bin/env python3
"""Evaluate one frozen evolution-round Skill Bank on the test split."""
import argparse
import json
import os
import time
from pathlib import Path

from skillexpand.l2.loop import RunLock
from skillexpand.l2.audit import audit_round
from skillexpand.evaluation.audit import audit_test

from skillexpand import schema as S
from skillexpand.evaluation.routing import FrozenRoutes
from skillexpand.evaluation.validation import FixedSkillScorer, ScoreCache, library_fingerprint
from skillexpand.l1.cold_start import freeze
from skillexpand.l1.runner import save
from skillexpand.persistence.store import SkillLibrary
from skillexpand.persistence.artifacts import load_cold_start, code_signature, provider_signature


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--round', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--test-workers', type=int, default=256)
    p.add_argument('--smoke', action='store_true')
    args = p.parse_args()
    if not 1 <= args.test_workers <= 256:
        raise ValueError('test-workers must be between 1 and 256')
    run = args.run_dir.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = RunLock(output / 'evaluation.lock')
    lock.acquire()
    cfg, plan, initial, _ = load_cold_start(run)
    os.environ['EXPE_CONFIG_FILE'] = str(run / 'config.json')
    os.environ['EXPE_TASK_FILE'] = cfg.benchmark.task_file
    library = SkillLibrary(run / 'skills.jsonl', benchmark=plan.benchmark)
    summary = json.loads((run / 'evolution' / f'round-{args.round}' / 'summary.json').read_text())
    audit_round(run, args.round)
    keys = summary['skills']
    skills = tuple(library.get(key) for _, key in sorted(keys.items()))
    if any(s.version != int(s.key.rsplit('@v', 1)[1]) for s in skills):
        raise ValueError('Invalid Skill version history')
    if {s.skill_id: s.key for s in skills} != keys:
        raise ValueError('Snapshot does not match round summary')
    freeze(output / 'library.json', [S.to_dict(s) for s in skills])
    protocol_path = output / 'protocol.json'
    # Worker count is scheduling metadata, not part of the scorer cache key.
    # Keep the first value written for resumable evaluation metadata.
    original_test_workers = (json.loads(protocol_path.read_text()).get(
        'test_workers', args.test_workers) if protocol_path.exists()
        else args.test_workers)
    freeze(protocol_path, {
        'round': args.round, 'library_hash': library_fingerprint(skills),
        'code': code_signature(), 'provider': provider_signature(),
        'routing_reference': [S.to_dict(s) for s in initial],
        'test_workers': original_test_workers,
        'execution': 'fixed-skill-single-attempt-v1',
    })
    save(output / 'executions' / f'{time.time_ns()}-{os.getpid()}.json',
         {'pid': os.getpid(), 'test_workers': args.test_workers, 'started': time.time(),
          'round': args.round, 'original_test_workers': original_test_workers})
    routes = FrozenRoutes(cfg, plan, initial, run / 'routes', S.SPLIT_TEST,
                          args.test_workers)
    for task in routes.ids:
        routes._add(json.loads((run / 'routes' / S.SPLIT_TEST / 'tasks' / f'{task}.json').read_text()))
    if set(routes.records) != set(routes.ids):
        raise ValueError('Missing frozen test routes')
    scorer = FixedSkillScorer(cfg, ScoreCache(output / 'scores.jsonl'), routes,
                              args.test_workers)
    freeze(output / 'score_protocol.json', {'hash': scorer.protocol_hash})
    per_skill = {}
    failed_skills = []
    for skill in skills:
        panel = f'test:{routes.fingerprint}:{skill.skill_id}'
        ids = routes.groups[skill.skill_id]
        if args.smoke:
            ids = ids[:1]
        try:
            result = scorer.score(skill, ids, panel)
            per_skill[skill.skill_id] = {'tasks': result.n, 'successes': result.successes,
                                         'score': result.score}
        except RuntimeError as exc:
            # A transient provider failure must not prevent other frozen route
            # groups from making progress. Failed units remain uncached and are
            # retried by the next --resume invocation.
            failed_skills.append({'skill_id': skill.skill_id, 'error': str(exc)})
            save(output / 'progress.json', {'status': 'incomplete',
                 'completed_skills': per_skill, 'failed_skills': failed_skills})
            continue
        save(output / 'progress.json', {'status': 'incomplete',
             'completed_skills': per_skill, 'failed_skills': failed_skills})
    if failed_skills:
        raise RuntimeError(f'Incomplete Skill evaluation; failed groups: {failed_skills}')
        save(output / 'skills' / f'{skill.skill_id}.json', per_skill[skill.skill_id])
    total = len(plan.tasks_in(S.SPLIT_TEST))
    if args.smoke:
        total = sum(item['tasks'] for item in per_skill.values())
    successes = sum(item['successes'] for item in per_skill.values())
    result = {'status': 'complete', 'benchmark': plan.benchmark, 'split': 'test',
              'evolution_round': args.round, 'library_hash': library_fingerprint(skills),
              'routing_reference': 'initial_skills', 'tasks': total, 'successes': successes,
              'score': successes / total if total else None,
              'per_skill': per_skill, 'routing_failures': list(routes.failed_task_ids),
              'test_workers': args.test_workers}
    save(output / 'summary.json', result)
    if args.smoke:
        save(output / 'smoke.json', {'status': 'passed', 'tasks': sum(x['tasks'] for x in per_skill.values())})
    else:
        save(output / 'audit.json', audit_test(run, output))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
