"""Continue original E5 production L2 after exact first-panel recovery."""
import argparse
import json
import os
import shutil
from pathlib import Path

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.l1.runner import save
from skillexpand.l2.loop import EvolutionConfig, LoopPaths, SerialEvolutionLoop
from skillexpand.persistence.artifacts import load_cold_start
from skillexpand.runtime.llm_relay import relay_from_env
from skillexpand.evaluation.routing import FrozenRoutes, RouteSpec, route_task
from skillexpand.evaluation.validation import ScoreCache


ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
SOURCE = ROOT / 'tb21-e5-qwen-deepseek-mixed-parallel-20261006-recovery3-candidate7'
CACHE = ROOT / 'tb21-e5-20261007-recovery14-task71-three-requests'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--repair-task28-card', action='store_true')
    args = parser.parse_args()
    out = args.run_dir.resolve()
    cfg, plan, initial, cold = load_cold_start(SOURCE)
    frozen = json.loads((SOURCE / 'l2_manifest.json').read_text())['config']
    config = EvolutionConfig(**frozen, evolve_rounds=1)
    assert frozen['autonomous_attempts'] == frozen['candidate_count'] == 3
    assert frozen['progressive_library'] and frozen['acceptance_panel'] == 'all_train'
    assert set(cold) == set(range(89))
    assert not list((SOURCE / 'l2_batches').glob('*.json'))
    cache_audit = json.loads((CACHE / 'audit.json').read_text())
    assert cache_audit['records'] == 356 and cache_audit['errors'] == 0
    assert cache_audit['proposal_id'] == '265cb41c55ef'
    if not out.exists():
        out.mkdir()
        for name in ('config.json', 'manifest.json', 'split.json', 'initial_skills.json',
                     'cold_start_complete.json', 'meta_skills.jsonl', 'skills.jsonl'):
            shutil.copy2(SOURCE / name, out / name)
        for name in ('discovery', 'routes') + (() if args.repair_task28_card else ('l2_patterns', 'l2_proposals')):
            shutil.copytree(SOURCE / name, out / name)
        (out / 'evolution/round-1').mkdir(parents=True)
        shutil.copytree(SOURCE / 'evolution/round-1/cards', out / 'evolution/round-1/cards')
        shutil.copy2(SOURCE / 'evolution/round-1/input.json', out / 'evolution/round-1/input.json')
        if args.repair_task28_card:
            recovered = ROOT / 'tb21-e3-20261007-recovery9-frozen-selector-stream'
            value = json.loads((recovered / 'evolution/round-1/cards/28.json').read_text())
            skill = json.loads((recovered / 'initial_skills.json').read_text())[0]
            assert skill['body'] == initial[0].body and skill['skill_id'] == initial[0].skill_id
            assert value['initial_skill_key'] == initial[0].key
            assert value['skill_load']['load_stage'] == 'after_selection'
            assert value['skill_load']['skill_id'] == value['selected_skill_id']
            save(out / 'task28_card_repair.json', {'source': str(recovered),
                'previous': json.loads((SOURCE / 'evolution/round-1/cards/28.json').read_text()),
                'new_card': value, 'same_skill_body': True,
                'proposals_regenerated': True, 'old_commit_not_imported': True})
            save(out / 'evolution/round-1/cards/28.json', value)
            repaired_routes = ROOT / 'tb21-e5-20261008-recovery18-provider503/routes/train'
            for name in ('tasks/28.json', 'complete.json'):
                shutil.copy2(repaired_routes / name, out / 'routes/train' / name)
        (out / 'train').mkdir()
        shutil.copy2(CACHE / 'train/predicted_scores.jsonl', out / 'train/predicted_scores.jsonl')
        save(out / 'recovery_ledger.json', {'stage': 'E5', 'source_run': str(SOURCE),
            'reviewer_source': str(CACHE), 'reused_records': 356, 'old_runs_read_only': True,
            'frozen_proposal_id': None if args.repair_task28_card else '265cb41c55ef',
            'task28_card_repair': args.repair_task28_card,
            'old_candidates_reusable_only_by_exact_reviewer_identity': True,
            'max_tokens_sent': False,
            'acceptance_route_recovery_task_ids': [] if args.repair_task28_card else [28],
            'scientific_scope': '89-task closed-set'})
        save(out / 'status.json', {'stage': 'E5', 'status': 'prepared'})
    assert json.loads((out / 'status.json').read_text())['status'] == 'prepared'
    if args.dry_run:
        # Production card audits verify original trajectory paths without rerunning L1.
        from skillexpand.benchmarks.terminalbench import audit_harbor_experience
        for task in range(89):
            card = S.from_dict(S.TaskExperience,
                json.loads((out / f'evolution/round-1/cards/{task}.json').read_text()))
            assert card.task_id == task and card.num_trials == 3
            audit_harbor_experience(card)
        print(json.dumps({'cards': 89, 'reused_reviewers': 356, 'new_l1_requests': 0,
                          'route_recovery': [28], 'batches': 2, 'max_tokens_sent': False}))
        return
    relay = None
    save(out / 'status.json', {'stage': 'E5', 'status': 'running', 'pid': os.getpid(),
                              'phase': 'acceptance_route_recovery'})
    (out / 'PID').write_text(str(os.getpid()) + '\n')
    try:
        os.environ.pop('EXPE_LLM_MAX_TOKENS', None)
        relay = relay_from_env()
        url = relay.start()
        cfg.benchmark.rollout.relay_base_url = url
        save(out / 'config.json', OmegaConf.to_container(cfg, resolve=True))
        manifest = json.loads((out / 'manifest.json').read_text())
        manifest['config'] = OmegaConf.to_container(cfg, resolve=True)
        save(out / 'manifest.json', manifest)
        os.environ.update(EXPE_CONFIG_FILE=str(out / 'config.json'), EXPE_TASK_FILE=cfg.benchmark.task_file,
            EXPE_LLM_BASE_URL=url, OPENAI_API_BASE=url, MODEL_API_BASE=url,
            EXPE_LLM_RELAY_REQUIRED='1', TBENCH_RELAY_BASE_URL=url, TBENCH_PERSIST_SANDBOXES='0')
        save(out / 'relay_manifest.json', {'transport': 'tencent_e2b_relay',
             'sandbox_id': relay.transport.sandbox_id, 'direct_provider_fallback': False, 'persistence': 0})
        loop = SerialEvolutionLoop(cfg, plan, LoopPaths(out), config)
        routes = FrozenRoutes.load_existing(cfg, plan, initial, out / 'routes', S.SPLIT_TRAIN)
        if args.repair_task28_card:
            assert not routes.failed_task_ids
        else:
            assert routes.failed_task_ids == (28,)
            record = route_task(RouteSpec(plan.benchmark, 28, routes.descriptions,
                str(out / 'routes/train/usage/28-recovery.json')))
            save(out / 'route28_recovery.json', {'previous': routes.records[28], 'recovery': record})
            assert not record.get('error') and record['selection']['ok'] and record['selection']['why'].strip()
            routes.records.pop(28)
            routes._add(record)
            save(out / 'routes/train/tasks/28.json', record)
        assert sum(map(len, routes.groups.values())) == 89
        save(out / 'routes/train/complete.json', {'fingerprint': routes.fingerprint,
            'tasks': 89, 'groups': routes.groups, 'failed_task_ids': []})
        scorer = loop._ensure_predicted_scorer()
        panel = f'val:{routes.fingerprint}:{initial[0].skill_id}'
        skills = [initial[0]] if args.repair_task28_card else [initial[0]] + [S.from_dict(S.CandidateSkill, json.loads(
            (out / f'l2_proposals/265cb41c55ef/candidate-{i}.json').read_text())['candidate']).skill
            for i in range(3)]
        for skill in skills:
            for task in range(89):
                key = ScoreCache.make_key(plan.benchmark, panel, task,
                    'predicted:' + scorer.protocol_hash, skill.body)
                row = scorer.cache.get(key)
                assert row and row['task_id'] == task and row['panel_key'] == panel
                assert row['protocol_hash'] == scorer.protocol_hash
                scorer._parse(json.dumps({k: row[k] for k in ('probability_true', 'predicted_success', 'reason')}))
        scorer.workers = 8
        save(out / 'cache_reconciliation.json', {'valid_records': len(skills) * 89, 'conflicts': [],
            'five_part_identity_checked': True, 'panel_key': panel, 'protocol_hash': scorer.protocol_hash})
        save(out / 'status.json', {'stage': 'E5', 'status': 'running', 'pid': os.getpid(), 'phase': 'production_L2'})
        result = loop.run_evolutions()
        assert result['completed_batches'] == result['batches'] == 2 and result['invalid_batches'] == 0
        save(out / 'result.json', result)
        save(out / 'audit.json', json.loads((out / 'evolution/round-1/audit.json').read_text()))
        save(out / 'status.json', {'stage': 'E5', 'status': 'complete', 'card_coverage': '89/89'})
        (out / 'recovery_summary.md').write_text(
            '# E5 production recovery\n\n89/89 cards and acceptance routes. '
            'Two production L2 batches completed and audited. See result.json, '
            'audit.json, cache_reconciliation.json and recovery_ledger.json.\n')
    except Exception as exc:
        save(out / 'status.json', {'stage': 'E5', 'status': 'needs_attention',
            'error': str(exc), 'error_class': getattr(exc, 'error_class', type(exc).__name__)})
        raise
    finally:
        if relay is not None:
            relay.close()


if __name__ == '__main__':
    main()
