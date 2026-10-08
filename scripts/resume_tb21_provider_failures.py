"""Resume frozen production L2 evidence in a new directory after provider 503."""
import argparse
import json
import os
import shutil
from pathlib import Path

from omegaconf import OmegaConf
from skillexpand.l1.runner import save
from skillexpand.l2.loop import EvolutionConfig, LoopPaths, SerialEvolutionLoop
from skillexpand.persistence.artifacts import load_cold_start, provider_signature, code_signature
from skillexpand.runtime.llm_relay import relay_from_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--stage', required=True, choices=('E3', 'E5'))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--recover-task60', action='store_true')
    args = parser.parse_args()
    src, out = args.source.resolve(), args.out.resolve()
    status = json.loads((src / 'status.json').read_text())
    assert status['status'] == 'needs_attention'
    assert 'ServiceUnavailableError' in status['error']
    if 'DiscoveryError' in status['error']:
        assert args.recover_task60 and args.stage == 'E3'
        assert src.name == 'tb21-e3-20261008-recovery18-provider503'
    manifest = json.loads((src / 'l2_manifest.json').read_text())
    assert manifest['code'] == code_signature(), 'Production code changed; audit before resuming'
    if not out.exists():
        shutil.copytree(src, out, ignore=shutil.ignore_patterns('PID', 'run.pid', 'campaign.lock'))
        save(out / 'status.json', {'stage': args.stage, 'status': 'prepared'})
        save(out / 'provider_recovery_ledger.json', {'source': str(src),
            'source_error': status['error'], 'old_runs_read_only': True,
            'frozen_experiment_config': manifest['config'], 'actual_review_workers': 8,
            'retry_strategy_unchanged': True, 'successful_cache_reused': True,
            'relay_runtime_rebound_in_new_directory': True})
    assert json.loads((out / 'status.json').read_text())['status'] == 'prepared'
    cfg, plan, _, _ = load_cold_start(out)
    cache = out / 'train/predicted_scores.jsonl'
    if args.dry_run:
        print(json.dumps({'stage': args.stage, 'reused_records': len(cache.read_text().splitlines()),
            'committed_batches': len(list((out / 'l2_batches').glob('*.json'))),
            'actual_review_workers': 8, 'source': str(src)}))
        return
    relay = None
    try:
        relay = relay_from_env()
        url = relay.start()
        cfg.benchmark.rollout.relay_base_url = url
        resolved = OmegaConf.to_container(cfg, resolve=True)
        save(out / 'config.json', resolved)
        cold_manifest = json.loads((out / 'manifest.json').read_text())
        cold_manifest['config'] = resolved
        save(out / 'manifest.json', cold_manifest)
        os.environ.update(EXPE_CONFIG_FILE=str(out / 'config.json'), EXPE_TASK_FILE=cfg.benchmark.task_file,
            EXPE_LLM_BASE_URL=url, OPENAI_API_BASE=url, MODEL_API_BASE=url,
            EXPE_LLM_RELAY_REQUIRED='1', TBENCH_RELAY_BASE_URL=url, TBENCH_PERSIST_SANDBOXES='0')
        manifest['runtime'] = resolved
        manifest['provider'] = provider_signature()
        save(out / 'l2_manifest.json', manifest)
        save(out / 'relay_manifest.json', {'transport': 'tencent_e2b_relay',
            'sandbox_id': relay.transport.sandbox_id, 'relay_base_url': url,
            'persistence': 0, 'direct_provider_fallback': False})
        save(out / 'status.json', {'stage': args.stage, 'status': 'running', 'pid': os.getpid()})
        (out / 'PID').write_text(str(os.getpid()) + '\n')
        loop = SerialEvolutionLoop(cfg, plan, LoopPaths(out), EvolutionConfig(**manifest['config'], evolve_rounds=1))
        scorer = loop._ensure_predicted_scorer()
        assert not loop.predicted_routes.failed_task_ids
        assert sum(map(len, loop.predicted_routes.groups.values())) == 89
        scorer.workers = 8
        if args.recover_task60:
            from skillexpand import schema as S
            from skillexpand.evaluation.validation import ScoreCache
            from skillexpand.runtime import agent_factory as F
            matches = []
            for path in (out / 'l2_proposals/4d2da4e8d098').glob('candidate-*.json'):
                candidate = json.loads(path.read_text()).get('candidate')
                if candidate and S.content_hash(candidate['skill']['body']) == '7809d5e59cc4':
                    matches.append(S.from_dict(S.CandidateSkill, candidate).skill)
            assert len(matches) == 1
            skill = matches[0]
            panel = f'val:{scorer.routes.fingerprint}:{skill.skill_id}'
            tasks = (4, 10, 22, 25, 32, 40, 65)
            infra_error = None
            try:
                scorer.score(skill, tasks, panel)
            except Exception as exc:
                infra_error = str(exc)
            save(out / 'infrastructure_recovery.json', {'task_ids': tasks, 'error': infra_error})
            usage = out / 'train/usage/predicted-terminalbench.terminalbench.general-60-7809d5e59cc4.requests.jsonl'
            records = [json.loads(line) for line in usage.read_text().splitlines()]
            used = sum(x.get('event') == 'start' and 'FORMAT CORRECTION:' in
                       json.dumps(x.get('prompts', [])) for x in records)
            assert used == 1
            save(out / 'task60_budget.json', {'used_before': used, 'corrections_this_run': 1,
                                            'original_max_corrections': 3})
            prompt = scorer.prompt(F.task_text_of(cfg, 60), skill)
            prompt += ('\n\nFORMAT CORRECTION: your previous visible answer did not '
                'match the required schema (LLM response did not contain a complete JSON object). '
                'Return only the single JSON object now; do not repeat the input or your analysis.')
            raw = scorer._call(loop._reasoning_host('l2_reviewer', out / 'train/usage/task60-correction2.json'), prompt)
            save(out / 'task60_correction2.json', {'raw': raw})
            parsed = scorer._parse(raw)
            key = ScoreCache.make_key(plan.benchmark, panel, 60, 'predicted:' + scorer.protocol_hash, skill.body)
            scorer.cache.put(key, {'task_id': 60, 'skill_key': skill.key, 'cache_key': key,
                'panel_key': panel, 'protocol_hash': scorer.protocol_hash,
                'format_attempts': 3, 'response_format': 'json_schema', **parsed})
            if infra_error:
                raise RuntimeError(infra_error)
        result = loop.run_evolutions()
        assert result['completed_batches'] == result['batches'] == 2
        assert result['invalid_batches'] == 0
        save(out / 'result.json', result)
        save(out / 'audit.json', json.loads((out / 'evolution/round-1/audit.json').read_text()))
        save(out / 'status.json', {'stage': args.stage, 'status': 'complete', 'card_coverage': '89/89'})
        (out / 'recovery_summary.md').write_text(
            f'# {args.stage} provider recovery\n\nTwo production batches completed and audited. '
            'Frozen evidence and successful cache reused; provider failures preserved in source. '
            'See provider_recovery_ledger.json, result.json and audit.json.\n')
    except Exception as exc:
        save(out / 'status.json', {'stage': args.stage, 'status': 'needs_attention',
             'error': str(exc), 'error_class': getattr(exc, 'error_class', type(exc).__name__)})
        raise
    finally:
        if relay is not None:
            relay.close()


if __name__ == '__main__':
    main()
