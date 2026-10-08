"""Restart closed-set L2 acceptance with a separate wire-format identity."""
import argparse
import json
import os
import shutil
from pathlib import Path

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.benchmarks.terminalbench import audit_harbor_experience
from skillexpand.l1.runner import save
from skillexpand.l2.loop import EvolutionConfig, LoopPaths, SerialEvolutionLoop
from skillexpand.persistence.artifacts import FILES, load_cold_start
from skillexpand.runtime.llm_relay import relay_from_env


def prepare(source, out, stage):
    cfg, plan, initial, cold = load_cold_start(source)
    frozen = json.loads((source / 'l2_manifest.json').read_text())['config']
    assert frozen['autonomous_attempts'] == frozen['candidate_count'] == 3
    assert frozen['progressive_library'] and frozen['acceptance_panel'] == 'all_train'
    assert set(cold) == set(range(89))
    identities = set()
    for task, card in cold.items():
        audit_harbor_experience(card)
        assert card.num_trials == 3
        for trial in card.l1_trials:
            key = (task, trial['index'])
            assert key not in identities
            identities.add(key)
    assert len(identities) == 267
    for task in range(89):
        card = S.from_dict(S.TaskExperience, json.loads(
            (source / f'evolution/round-1/cards/{task}.json').read_text()))
        assert card.task_id == task and card.num_trials == 3
        assert card.skill_load['load_stage'] == 'after_selection'
        audit_harbor_experience(card)
    out.mkdir(exist_ok=False)
    for name in FILES:
        if (source / name).exists():
            shutil.copy2(source / name, out / name)
    for name in ('discovery', 'routes', 'l2_patterns', 'l2_proposals'):
        shutil.copytree(source / name, out / name)
    (out / 'evolution/round-1').mkdir(parents=True)
    for name in ('cards',):
        shutil.copytree(source / 'evolution/round-1' / name,
                        out / 'evolution/round-1' / name)
    shutil.copy2(source / 'evolution/round-1/input.json', out / 'evolution/round-1/input.json')
    # Preserve M0; SkillLibrary initializes the original v0 separately.
    shutil.copy2(source / 'meta_skills.jsonl', out / 'meta_skills.jsonl')
    save(out / 'recovery_ledger.json', {
        'stage': stage, 'source': str(source), 'old_runs_read_only': True,
        'config': frozen, 'response_format_sent': False,
        'reviewer_scores_reused': 0, 'historical_commits_imported': 0,
        'reviewer_cache_identity_changed': True, 'raw_attempts': 267,
        'card_coverage': '89/89', 'new_l1_requests': 0, 'actual_review_workers': 8,
        'scope': '89-task closed-set train/evolution acceptance',
        'e3_task60_prior_corrections': 2 if stage == 'E3' else None,
        'e3_task60_remaining_corrections': 1 if stage == 'E3' else None,
    })
    save(out / 'status.json', {'stage': stage, 'status': 'prepared'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--stage', required=True, choices=('E3', 'E5'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    source, out = args.source.resolve(), args.out.resolve()
    if not out.exists():
        prepare(source, out, args.stage)
    assert json.loads((out / 'status.json').read_text())['status'] == 'prepared'
    ledger = json.loads((out / 'recovery_ledger.json').read_text())
    assert ledger['source'] == str(source) and ledger['stage'] == args.stage
    if args.dry_run:
        print(json.dumps(ledger))
        return
    os.environ['EXPE_REVIEWER_RESPONSE_FORMAT'] = 'omit'
    if args.stage == 'E5':
        os.environ.pop('EXPE_LLM_MAX_TOKENS', None)
    else:
        os.environ['EXPE_LLM_MAX_TOKENS'] = '32768'
    relay = None
    save(out / 'status.json', {'stage': args.stage, 'status': 'running',
                              'pid': os.getpid(), 'phase': 'relay_start'})
    (out / 'PID').write_text(str(os.getpid()) + '\n')
    try:
        cfg, plan, initial, _ = load_cold_start(out)
        relay = relay_from_env()
        url = relay.start()
        cfg.benchmark.rollout.relay_base_url = url
        resolved = OmegaConf.to_container(cfg, resolve=True)
        save(out / 'config.json', resolved)
        manifest = json.loads((out / 'manifest.json').read_text())
        manifest['config'] = resolved
        save(out / 'manifest.json', manifest)
        os.environ.update(EXPE_CONFIG_FILE=str(out / 'config.json'),
            EXPE_TASK_FILE=cfg.benchmark.task_file, EXPE_LLM_BASE_URL=url,
            OPENAI_API_BASE=url, MODEL_API_BASE=url, EXPE_LLM_RELAY_REQUIRED='1',
            TBENCH_RELAY_BASE_URL=url, TBENCH_PERSIST_SANDBOXES='0')
        save(out / 'relay_manifest.json', {'transport': 'tencent_e2b_relay',
            'sandbox_id': relay.transport.sandbox_id, 'direct_provider_fallback': False,
            'persistence': 0, 'reviewer_response_format': 'omit',
            'max_tokens_sent': args.stage == 'E3'})
        loop = SerialEvolutionLoop(cfg, plan, LoopPaths(out),
            EvolutionConfig(**ledger['config'], evolve_rounds=1))
        scorer = loop._ensure_predicted_scorer()
        scorer.workers = 8
        assert not scorer.routes.failed_task_ids
        assert sum(map(len, scorer.routes.groups.values())) == 89
        save(out / 'cache_reconciliation.json', {'protocol_hash': scorer.protocol_hash,
            'response_format': 'omit', 'old_scores_imported': 0, 'routes': 89})
        if args.stage == 'E3':
            original_review = scorer._review
            def bounded_review(host, prompt):
                payload = json.loads(prompt)
                if S.content_hash(payload['skill']['body']) != '7809d5e59cc4':
                    return original_review(host, prompt)
                from skillexpand.runtime import agent_factory as F
                if payload['task'] != F.task_text_of(cfg, 60):
                    return original_review(host, prompt)
                budget_path = out / 'task60_budget.json'
                assert not budget_path.exists(), 'Task 60 final correction already consumed'
                save(budget_path, {'used_before': 2, 'corrections_this_run': 1,
                                  'original_max_corrections': 3})
                corrected = prompt + ('\n\nFORMAT CORRECTION: Return only the single '
                    'JSON object; do not repeat the input or your analysis.')
                raw = scorer._call(host, corrected)
                save(out / 'task60_correction3.json', {'raw': raw})
                return scorer._parse(raw), 4
            scorer._review = bounded_review
        save(out / 'status.json', {'stage': args.stage, 'status': 'running',
            'pid': os.getpid(), 'phase': 'production_L2', 'card_coverage': '89/89'})
        result = loop.run_evolutions()
        assert result['completed_batches'] == result['batches'] == 2
        assert result['invalid_batches'] == 0
        save(out / 'result.json', result)
        save(out / 'audit.json', json.loads((out / 'evolution/round-1/audit.json').read_text()))
        save(out / 'status.json', {'stage': args.stage, 'status': 'complete',
                                  'card_coverage': '89/89'})
    except Exception as exc:
        save(out / 'status.json', {'stage': args.stage, 'status': 'needs_attention',
            'error': str(exc), 'error_class': getattr(exc, 'error_class', type(exc).__name__)})
        raise
    finally:
        if relay is not None:
            relay.close()


if __name__ == '__main__':
    main()
