#!/usr/bin/env python3
"""Evaluate one frozen Skill Bank snapshot (round 0 = cold start) on the test split.

The run's frozen test routes are reused, so every snapshot is measured on the
same task-to-family assignment as the canonical ``--phase test`` evaluation.
"""
import argparse
import json
import os
import time
from pathlib import Path

from skillexpand import schema as S
from skillexpand.evaluation.routing import FrozenRoutes
from skillexpand.evaluation.snapshots import evaluate_library
from skillexpand.l1.artifacts import load_cold_start
from skillexpand.l2.audit import audit_round
from skillexpand.persistence.io import RunLock, save
from skillexpand.persistence.store import SkillLibrary


def snapshot(run, initial, plan, round_index):
    """Return the recorded round-end library; never guess versions from round numbers."""
    if round_index == 0:
        return tuple(sorted(initial, key=lambda s: s.skill_id))
    summary = json.loads((run / 'evolution' / f'round-{round_index}' / 'summary.json').read_text())
    audit_round(run, round_index)
    library = SkillLibrary(run / 'skills.jsonl', benchmark=plan.benchmark)
    keys = summary['skills']
    skills = tuple(library.get(key) for _, key in sorted(keys.items()))
    if any(s is None or s.version != int(s.key.rsplit('@v', 1)[1]) for s in skills):
        raise ValueError('Invalid Skill version history')
    if {s.skill_id: s.key for s in skills} != keys:
        raise ValueError('Snapshot does not match round summary')
    descriptions = {s.skill_id: s.description for s in initial}
    if any(s.description != descriptions.get(s.skill_id) for s in skills):
        raise ValueError('Snapshot changes frozen routing descriptions')
    return skills


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--round', type=int, required=True, help='0 evaluates the cold-start library')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--test-workers', type=int, default=256)
    p.add_argument('--smoke', action='store_true')
    p.add_argument('--allow-code-change', action='store_true')
    args = p.parse_args()
    if not 1 <= args.test_workers <= 256:
        raise ValueError('test-workers must be between 1 and 256')
    if args.round < 0:
        raise ValueError('round must be nonnegative')
    run = args.run_dir.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with RunLock(output / 'evaluation.lock'):
        cfg, plan, initial, _ = load_cold_start(run)
        os.environ['EXPE_CONFIG_FILE'] = str(run / 'config.json')
        os.environ['EXPE_TASK_FILE'] = cfg.benchmark.task_file
        skills = snapshot(run, initial, plan, args.round)
        if not (run / 'routes' / S.SPLIT_TEST / 'complete.json').exists():
            raise ValueError('Freeze test routes with --phase test before evaluating snapshots')
        # The constructor re-checks the frozen route manifest; records are then
        # loaded without writing anything below the training run directory.
        FrozenRoutes(cfg, plan, initial, run / 'routes', S.SPLIT_TEST, args.test_workers)
        routes = FrozenRoutes.load_existing(cfg, plan, initial, run / 'routes', S.SPLIT_TEST)
        save(output / 'executions' / f'{time.time_ns()}-{os.getpid()}.json',
             {'pid': os.getpid(), 'test_workers': args.test_workers, 'started': time.time(),
              'round': args.round})
        result = evaluate_library(
            cfg, plan, run, skills, initial, routes, output, args.test_workers,
            protocol_extra={'round': args.round, 'execution': 'fixed-skill-single-attempt-v1'},
            summary_extra={'benchmark': plan.benchmark, 'evolution_round': args.round},
            smoke=args.smoke, allow_code_change=args.allow_code_change)
        if args.smoke:
            save(output / 'smoke.json', {'status': 'passed', 'tasks': result['tasks']})
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
