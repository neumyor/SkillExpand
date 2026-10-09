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
from skillexpand.persistence.artifacts import FILES, load_cold_start, code_signature, provider_signature
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
    parser.add_argument('--resume-e3-empty-answers', action='store_true')
    args = parser.parse_args()
    source, out = args.source.resolve(), args.out.resolve()
    if not out.exists():
        if args.resume_e3_empty_answers:
            assert args.stage == 'E3'
            assert source.name == 'tb21-e3-20261008-recovery20-noformat'
            assert json.loads((source / 'status.json').read_text())['status'] == 'needs_attention'
            assert json.loads((source / 'l2_manifest.json').read_text())['code'] == code_signature()
            shutil.copytree(source, out, ignore=shutil.ignore_patterns('PID', 'run.pid', 'campaign.lock'))
            ledger = json.loads((out / 'recovery_ledger.json').read_text())
            ledger.update(source=str(source), resumed_same_protocol=True,
                          reused_exact_scores=86, empty_answer_tasks=[20, 71, 81])
            save(out / 'recovery_ledger.json', ledger)
            save(out / 'status.json', {'stage': args.stage, 'status': 'prepared'})
        else:
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
        if args.resume_e3_empty_answers:
            l2_manifest = json.loads((out / 'l2_manifest.json').read_text())
            assert l2_manifest['code'] == code_signature()
            l2_manifest.update(runtime=resolved, provider=provider_signature())
            save(out / 'l2_manifest.json', l2_manifest)
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
                if args.resume_e3_empty_answers and payload['skill']['body'] == initial[0].body:
                    from skillexpand.runtime import agent_factory as F
                    task = next((t for t in (20, 71, 81)
                                 if payload['task'] == F.task_text_of(cfg, t)), None)
                    if task is not None:
                        usage = list((source / 'train/usage').glob(f'predicted-*-{task}-*.requests.jsonl'))
                        assert len(usage) == 1
                        starts = [json.loads(line) for line in usage[0].read_text().splitlines()
                                  if json.loads(line).get('event') == 'start']
                        assert len(starts) == 2
                        assert sum('FORMAT CORRECTION:' in json.dumps(r['prompts']) for r in starts) == 1
                        budget = out / f'task{task}_base_format_budget.json'
                        assert not budget.exists(), 'Remaining correction budget already consumed'
                        corrected = prompt + ('\n\nFORMAT CORRECTION: Previous visible answers '
                            'were empty. Return only the required single JSON object.')
                        for correction in (2, 3):
                            save(budget, {'used_before': 1, 'corrections_this_run': correction - 1,
                                          'original_max_corrections': 3})
                            raw = scorer._call(host, corrected)
                            save(out / f'task{task}_base_correction{correction}.json', {'raw': raw})
                            try:
                                return scorer._parse(raw), correction + 1
                            except (ValueError, RuntimeError):
                                if correction == 3:
                                    raise
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
