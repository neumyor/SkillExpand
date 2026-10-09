#!/usr/bin/env python3
"""Build a progressive-library cold-start ledger from raw Harbor rollouts.

The ledger holds one skill-free schema-5 card per task, a single hand-written
bootstrap Skill, and a frozen config that carries ``benchmark.progressive_library``
so ``load_cold_start`` and the offline audit recognise it.  There is no family
clustering and no task->Skill map: Skills are chosen at execution time.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from skillexpand import schema as S
from skillexpand.l1 import learning as L
from skillexpand.l1 import protocol as P
from skillexpand.persistence.io import freeze

BOOTSTRAP_NAME = 'TerminalBench repository repair bootstrap'
BOOTSTRAP_DEFINITION = 'Reusable command-line inspection, repair, and verifier workflow.'
BOOTSTRAP_BODY = ('1. Inspect the repository and task files.\n'
                  '2. Make the smallest repair supported by observed evidence.\n'
                  '3. Run the provided verifier before finishing.')


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source-root', type=Path, required=True,
                    help='directory of Harbor trial dirs (<task>__<id>/result.json)')
    ap.add_argument('--task-file', type=Path, required=True)
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--source-model', default='openai/qwen3.6-flash-distill')
    ap.add_argument('--method-model', default=None,
                    help='model for every method role (default: --source-model)')
    ap.add_argument('--expected-tasks', type=int, default=89)
    ap.add_argument('--attempts', type=int, default=3)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--bootstrap-family', default='general')
    ap.add_argument('--card-batch-size', type=int, default=12)
    ap.add_argument('--runner-script', default=None,
                    help="Harbor runner (default: the terminalbench benchmark config's)")
    return ap


def default_runner():
    from skillexpand.runtime import agent_factory as F
    return str(F.load_config('terminalbench').benchmark.rollout.runner_script)


def main(argv=None):
    args = parser().parse_args(argv)
    source, root = args.source_root.resolve(), args.run_dir.resolve()
    rows = json.loads(args.task_file.read_text())
    by_name = {r['task_name']: r for r in rows}
    grouped = defaultdict(list)
    for rp in sorted(source.glob('*/result.json')):
        d = json.loads(rp.read_text())
        if d.get('task_name') in by_name:
            grouped[d['task_name']].append((rp.parent, d))
    names = sorted(by_name)
    short = {n: len(grouped[n]) for n in names if len(grouped[n]) != args.attempts}
    if len(names) != args.expected_tasks or short:
        raise SystemExit(f'expected {args.expected_tasks} tasks x {args.attempts} attempts; '
                         f'task count {len(names)}, wrong attempt counts {short}')
    n_tasks, n_trials = len(names), len(names) * args.attempts
    family = args.bootstrap_family
    skill_id = f'terminalbench.{family}'
    assignment = {i: S.SPLIT_TRAIN for i in range(n_tasks)}
    train_ids = list(range(n_tasks))
    task_rows = [{'task': by_name[n]['instruction'],
                  'env_kwargs': {'instruction': by_name[n]['instruction'], 'task_name': n},
                  'env_name': 'terminalbench'} for n in names]
    split = S.SplitPlan.make(assignment, 'terminalbench', args.seed)
    cards, exps, trial_manifest = {}, {}, []
    for i, n in enumerate(names):
        trials, rewards = [], []
        for idx, (td, d) in enumerate(sorted(grouped[n], key=lambda x: x[0].name), 1):
            reward = bool((d.get('verifier_result') or {}).get('rewards', {}).get('reward', 0))
            rewards.append(reward)
            remote = str(d.get('trial_uri', '')).removeprefix('file://')
            local = str(td / 'agent' / 'trajectory.json')
            trial_manifest.append({
                'task_id': i, 'task_name': n, 'attempt_index': idx, 'reward': int(reward),
                'source_model': args.source_model, 'trial_dir': remote or str(td),
                'result_path': remote + '/result.json' if remote else str(td / 'result.json'),
                'trajectory_path': remote + '/agent/trajectory.json' if remote else local})
            tpath = td / 'agent' / 'trajectory.json'
            steps = json.loads(tpath.read_text()).get('steps', []) if tpath.exists() else []
            events = [{'ref': f'e{j + 1}', 'action': 'TerminalBatch',
                       'observation': str(s.get('message') or s.get('observation') or ''),
                       'environment': {'success': False}}
                      for j, s in enumerate(steps) if s.get('message') or s.get('observation')]
            if not events:
                events = [{'ref': 'e1', 'action': 'TerminalBatch',
                           'observation': 'No trajectory steps recorded.',
                           'environment': {'success': False}}]
            trials.append({'index': idx, 'phase': 'autonomous', 'status': 'completed',
                           'success': reward, 'termination': 'verifier',
                           'trajectory': remote or local, 'events': events})
        instruction = by_name[n]['instruction']
        evidence = P.evidence(instruction, trials)
        solved = any(rewards)
        eid = f'discovery:{skill_id}:{i}'
        card = L.card(i, instruction, trials, {'status': 'valid', 'claims': []},
                      'external_harbor', 'tb21-e1-batch', len, evidence=evidence,
                      card_id=eid, benchmark='terminalbench', family_id=family)
        exps[i] = S.TaskExperience(
            eid, 'terminalbench', i, instruction, family, S.SPLIT_TRAIN, solved,
            args.attempts, failed_trajectories=tuple(t['trajectory'] for t in trials if not t['success']),
            final_trajectory=trials[-1]['trajectory'] if solved else None,
            selection_source=S.SELECTION_UNSKILLED, trial_rewards=tuple(rewards),
            trial_phases=('autonomous',) * args.attempts, experience_card=card,
            l1_trials=tuple(trials))
        cards[i] = card
    initial = S.Skill(
        skill_id, family, 0, BOOTSTRAP_NAME, BOOTSTRAP_DEFINITION, BOOTSTRAP_BODY,
        S.Provenance(rationale='Bootstrap only; replace with model-generated proposals before formal E1',
                     source_experience_ids=tuple(exps[i].experience_id for i in train_ids),
                     source_task_ids=tuple(train_ids)))
    method_model = args.method_model or args.source_model
    config = {
        'benchmark': {
            'name': 'terminalbench', 'task_prefix': '', 'task_file': str(args.task_file.resolve()),
            'max_steps': 1, 'num_fewshots': 0, 'ai_name': 'terminal agent',
            'env': {'show_admissible_commands': False},
            'l1': {'adapter': 'skillexpand.l1.adapters:TerminalBenchAdapter'},
            'progressive_library': True,
            'rollout': {'mode': 'harbor_rollout',
                        'runner_script': args.runner_script or default_runner()}},
        'agent': {'llm': method_model},
        'models': {k: method_model for k in
                   ('l1_executor', 'cold_start', 'l2_planner', 'l2_editor', 'l2_reviewer', 'selector')}}
    initial_payload = [S.to_dict(initial)]
    hashes = {str(i): S.content_hash(P.projection(cards[i])) for i in train_ids}
    manifest = {
        'protocol': 'progressive-library-experience-first', 'split': S.to_dict(split),
        'config': config, 'task_table_hash': S.content_hash(task_rows), 'prompts': {},
        'k': args.attempts, 'supervised': False, 'supervised_attempts': 0,
        'card_batch_size': args.card_batch_size, 'skill_edit_mode': 'rewrite',
        'routing_mode': 'progressive_library', 'acceptance_panel': 'all_train',
        'code': {'importer': 'tb-eval-import-v1'},
        'trajectory_import': {
            'source_root': str(source), 'source_model': args.source_model,
            'trial_count': n_trials, 'valid_rollout_count': None,
            'coverage': f'{n_tasks} tasks x {args.attempts} attempts raw; '
                        'validity recorded by input_coverage.json'}}
    freeze(root / 'config.json', config)
    freeze(root / 'split.json', S.to_dict(split))
    freeze(root / 'initial_skills.json', initial_payload)
    freeze(root / 'cold_start_complete.json', {
        'protocol': 'progressive-library', 'train_count': len(train_ids),
        'initial_skills_hash': S.content_hash(initial_payload)})
    freeze(root / 'manifest.json', manifest)
    freeze(root / 'discovery/card_hashes.json', hashes)
    for i, e in exps.items():
        freeze(root / 'discovery/results' / f'{i}.json', S.to_dict(e))
    freeze(root / 'discovery/initial_skills' / f'{family}-0-patterns.json',
           {'card_hashes': hashes, 'raw': None, 'patterns': [], 'status': 'bootstrap'})
    freeze(root / 'discovery/initial_skills' / f'{family}-0.json', S.to_dict(initial))
    skills_path = root / 'skills.jsonl'
    skill_line = S.to_jsonl(initial) + '\n'
    if skills_path.exists() and skills_path.read_text() != skill_line:
        raise ValueError(f'frozen artifact changed: {skills_path}')
    if not skills_path.exists():
        skills_path.write_text(skill_line)
    (root / 'trial_manifest.jsonl').write_text(''.join(
        json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n'
        for x in sorted(trial_manifest, key=lambda x: (x['task_id'], x['attempt_index']))))
    summary = {
        'status': 'cold_start_imported', 'benchmark': 'terminalbench', 'tasks': n_tasks,
        'trials': n_trials, 'train_tasks': len(train_ids),
        'reward_1': sum(r['reward'] for r in trial_manifest),
        'source_model': args.source_model,
        'coverage': f'{n_tasks} tasks x {args.attempts} attempts raw'}
    freeze(root / 'batch_import_summary.json', summary)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
