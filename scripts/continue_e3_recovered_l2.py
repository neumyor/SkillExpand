"""Continue frozen E3 L2 with audited recovered cards in an independent run."""
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
from skillexpand.persistence.artifacts import load_cold_start
from skillexpand.runtime.llm_relay import relay_from_env
from skillexpand.evaluation.routing import FrozenRoutes, RouteSpec, route_task
from skillexpand.evaluation.validation import ScoreCache


ROOT = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
SOURCE = ROOT / 'tb21-e3-deepseek-parallel-20261006-recovery5'
RECOVERY = ROOT / 'tb21-e3-20261007-recovery9-frozen-selector-stream'


def prepare(out, l2_source=None):
    cfg, plan, initial, cold = load_cold_start(SOURCE)
    frozen = json.loads((SOURCE / 'l2_manifest.json').read_text())['config']
    assert frozen['autonomous_attempts'] == 3
    assert frozen['candidate_count'] == 3
    assert frozen['progressive_library'] and frozen['acceptance_panel'] == 'all_train'
    assert set(cold) == set(range(89))
    assert json.loads((RECOVERY / 'status.json').read_text())['task28_recovery'] == 'complete'
    cards = {}
    identities = set()
    for task in range(89):
        card = json.loads((RECOVERY / f'evolution/round-1/cards/{task}.json').read_text())
        if task != 28:
            assert card == json.loads((SOURCE / f'evolution/round-1/cards/{task}.json').read_text())
        exp = S.from_dict(S.TaskExperience, card)
        skill = next(s for s in initial if s.skill_id == exp.selected_skill_id)
        assert exp.task_id == task and exp.evolution_round == 1
        assert exp.initial_skill_key == skill.key and exp.num_trials == 3
        assert exp.selection_source == S.SELECTION_AGENT
        assert exp.skill_load['load_stage'] == 'after_selection'
        audit_harbor_experience(exp)
        for trial in exp.l1_trials:
            key = (task, trial['index'])
            assert key not in identities
            identities.add(key)
        cards[task] = card
    assert len(identities) == 267
    out.mkdir(exist_ok=False)
    for name in ('config.json', 'manifest.json', 'split.json', 'initial_skills.json',
                 'cold_start_complete.json', 'input_coverage.json'):
        shutil.copy2(SOURCE / name, out / name)
    shutil.copytree(SOURCE / 'discovery', out / 'discovery')
    (out / 'evolution/round-1').mkdir(parents=True)
    shutil.copy2(SOURCE / 'evolution/round-1/input.json', out / 'evolution/round-1/input.json')
    for task, card in cards.items():
        save(out / f'evolution/round-1/cards/{task}.json', card)
    if l2_source is not None:
        assert l2_source in {
            ROOT / 'tb21-e3-20261007-recovery10-production-l2',
            ROOT / 'tb21-e3-20261007-recovery11-l2-missing',
            ROOT / 'tb21-e3-20261007-recovery12-l2-task45',
            ROOT / 'tb21-e3-20261007-recovery13-frozen-l2',
            ROOT / 'tb21-e3-20261007-recovery14-503-then-task71',
        }
        assert not list((l2_source / 'l2_batches').glob('*.json'))
        for task, card in cards.items():
            assert card == json.loads((l2_source / f'evolution/round-1/cards/{task}.json').read_text())
        source_config = json.loads((l2_source / 'l2_manifest.json').read_text())['config']
        assert source_config == frozen
        for name in ('routes', 'train', 'l2_patterns', 'l2_proposals'):
            shutil.copytree(l2_source / name, out / name)
        shutil.copy2(l2_source / 'meta_skills.jsonl', out / 'meta_skills.jsonl')
        shutil.copy2(l2_source / 'skills.jsonl', out / 'skills.jsonl')
    save(out / 'recovery_ledger.json', {
        'stage': 'E3', 'cold_start_source': str(SOURCE), 'task28_source': str(RECOVERY),
        'reused_task_ids': [t for t in range(89) if t != 28], 'recovered_task_ids': [28],
        'old_runs_read_only': True, 'card_coverage': '89/89', 'skill_aware_attempts': 267,
        'frozen_evolution_config': frozen, 'historical_errors': str(SOURCE / 'evolution/round-1/errors'),
        'scientific_scope': '89-task closed-set train/evolution acceptance panel',
        'l2_recovery_source': str(l2_source) if l2_source else None,
    })
    save(out / 'status.json', {'stage': 'E3', 'status': 'prepared',
                              'card_coverage': '89/89', 'l2_complete': False})
    return cfg, plan, EvolutionConfig(**frozen, evolve_rounds=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--l2-recovery-source', type=Path)
    args = parser.parse_args()
    out = args.run_dir.resolve()
    if out.exists():
        status = json.loads((out / 'status.json').read_text())
        assert status['status'] == 'prepared', 'Only a prepared run may be started'
        cfg, plan, _, _ = load_cold_start(out)
        frozen = json.loads((out / 'recovery_ledger.json').read_text())['frozen_evolution_config']
        config = EvolutionConfig(**frozen, evolve_rounds=1)
    else:
        cfg, plan, config = prepare(out, args.l2_recovery_source.resolve()
                                     if args.l2_recovery_source else None)
    if args.dry_run:
        print(json.dumps({'run': str(out), 'cards': 89, 'attempts': 267,
                          'new_l1_requests': 0, 'l2_batches': 2, 'config': config.to_dict()}))
        return
    relay = None
    save(out / 'status.json', {'stage': 'E3', 'status': 'running', 'pid': os.getpid(),
                              'card_coverage': '89/89', 'phase': 'L2'})
    (out / 'PID').write_text(str(os.getpid()) + '\n')
    try:
        relay = relay_from_env()
        url = relay.start()
        cfg.benchmark.rollout.relay_base_url = url
        resolved = OmegaConf.to_container(cfg, resolve=True)
        save(out / 'config.json', resolved)
        manifest = json.loads((out / 'manifest.json').read_text())
        manifest['config'] = resolved
        save(out / 'manifest.json', manifest)
        os.environ.update(EXPE_CONFIG_FILE=str(out / 'config.json'),
                          EXPE_TASK_FILE=cfg.benchmark.task_file,
                          EXPE_LLM_BASE_URL=url, OPENAI_API_BASE=url, MODEL_API_BASE=url,
                          EXPE_LLM_RELAY_REQUIRED='1', TBENCH_RELAY_BASE_URL=url,
                          TBENCH_PERSIST_SANDBOXES='0')
        save(out / 'relay_manifest.json', {'llm_transport': 'tencent_e2b_relay',
             'relay_base_url': url, 'sandbox_id': relay.transport.sandbox_id,
             'direct_provider_fallback': False, 'persistence': 0})
        loop = SerialEvolutionLoop(cfg, plan, LoopPaths(out), config)
        ledger = json.loads((out / 'recovery_ledger.json').read_text())
        if ledger.get('l2_recovery_source'):
            routes = FrozenRoutes.load_existing(cfg, plan, loop.initial, out / 'routes', S.SPLIT_TRAIN)
            for task in routes.failed_task_ids:
                old = routes.records[task]
                record = route_task(RouteSpec(plan.benchmark, task, routes.descriptions,
                    str(out / 'routes/train/usage' / f'{task}-recovery.json')))
                save(out / 'routes/train/recovery_evidence' / f'{task}.json',
                     {'previous': old, 'recovery': record, 'source': ledger['l2_recovery_source']})
                assert not record.get('error') and record['selection']['ok'], record
                assert record['selection']['why'].strip(), record
                routes.records.pop(task)
                routes._add(record)
                save(out / 'routes/train/tasks' / f'{task}.json', record)
            assert not routes.failed_task_ids
            assert sum(map(len, routes.groups.values())) == 89
            save(out / 'routes/train/complete.json', {'fingerprint': routes.fingerprint,
                'tasks': 89, 'groups': routes.groups, 'failed_task_ids': []})
            ledger['acceptance_route_coverage'] = '89/89'
            save(out / 'recovery_ledger.json', ledger)
            source_name = Path(ledger['l2_recovery_source']).name
            if source_name in {'tb21-e3-20261007-recovery12-l2-task45',
                               'tb21-e3-20261007-recovery13-frozen-l2'}:
                scorer = loop._ensure_predicted_scorer()
                candidates = []
                for path in (out / 'l2_proposals').glob('*/candidate-*.json'):
                    value = json.loads(path.read_text()).get('candidate')
                    if value and S.content_hash(value['skill']['body']) == '3115ca861f9b':
                        candidates.append(S.from_dict(S.CandidateSkill, value).skill)
                assert len(candidates) == 1
                skill = candidates[0]
                panel = f'val:{scorer.routes.fingerprint}:{skill.skill_id}'
                key = ScoreCache.make_key(plan.benchmark, panel, 71,
                    'predicted:' + scorer.protocol_hash, skill.body)
                usage = out / 'train/usage' / f'predicted-{skill.skill_id}-71-3115ca861f9b.requests.jsonl'
                records = [json.loads(line) for line in usage.read_text().splitlines()]
                used = sum(r.get('event') == 'start' and 'FORMAT CORRECTION:' in
                           json.dumps(r.get('prompts', [])) for r in records)
                infrastructure_error = None
                if source_name == 'tb21-e3-20261007-recovery13-frozen-l2':
                    previous_ledger = json.loads((Path(ledger['l2_recovery_source']) /
                                                  'recovery_ledger.json').read_text())
                    used = previous_ledger['task71_format_corrections_used_before'] + previous_ledger['task71_format_corrections_this_run']
                    assert used == 2, 'Task 71 correction accounting changed'
                    task_ids = (13, 31, 36, 63, 65, 72, 75, 81, 84)
                    ledger['infrastructure_recovery_tasks'] = list(task_ids)
                    ledger['recovery_order'] = ['provider_503_tasks', 'task71_format_correction']
                    save(out / 'recovery_ledger.json', ledger)
                    save(out / 'status.json', {'stage': 'E3', 'status': 'running',
                         'pid': os.getpid(), 'phase': 'recover_503', 'task_ids': list(task_ids)})
                    try:
                        scorer.score(skill, task_ids, panel)
                    except Exception as exc:
                        infrastructure_error = str(exc)
                    save(out / 'infrastructure_recovery.json', {'task_ids': list(task_ids),
                        'error': infrastructure_error,
                        'successful_task_ids': [t for t in task_ids if scorer.cache.get(
                            ScoreCache.make_key(plan.benchmark, panel, t,
                                                'predicted:' + scorer.protocol_hash, skill.body))]})
                else:
                    assert used == 1, 'Task 71 correction accounting changed'
                ledger['task71_format_corrections_used_before'] = used
                ledger['task71_format_corrections_this_run'] = 1
                ledger['task71_max_format_corrections'] = 3
                save(out / 'recovery_ledger.json', ledger)
                if scorer.cache.get(key) is None:
                    save(out / 'status.json', {'stage': 'E3', 'status': 'running',
                         'pid': os.getpid(), 'phase': 'task71_format_correction', 'task_ids': [71]})
                    from skillexpand.runtime import agent_factory as F
                    prompt = scorer.prompt(F.task_text_of(cfg, 71), skill)
                    prompt += ('\n\nFORMAT CORRECTION: your previous visible answer did not '
                        'match the required schema (LLM response did not contain a complete JSON object). '
                        'Return only the single JSON object now; do not repeat the input or your analysis.')
                    host = loop._reasoning_host('l2_reviewer', out / f'train/usage/task71-correction{used+1}.json')
                    raw = scorer._call(host, prompt)
                    save(out / f'task71_correction{used+1}.json', {'raw': raw, 'cache_key': key})
                    parsed = scorer._parse(raw)
                    scorer.cache.put(key, {'task_id': 71, 'skill_key': skill.key,
                        'cache_key': key, 'panel_key': panel, 'protocol_hash': scorer.protocol_hash,
                        'format_attempts': used + 2, 'response_format': 'json_schema', **parsed})
                if infrastructure_error:
                    raise RuntimeError(infrastructure_error)
        result = loop.run_evolutions()
        audit = json.loads((out / 'evolution/round-1/audit.json').read_text())
        assert result['completed_batches'] == result['batches'] == 2
        assert result['train_cards'] == 89 and result['status'] == 'complete'
        assert result['invalid_batches'] == 0, 'Invalid L2 batches require attention'
        routes_complete = json.loads((out / 'routes/train/complete.json').read_text())
        assert not routes_complete['failed_task_ids'], 'Acceptance routes require attention'
        assert sum(map(len, routes_complete['groups'].values())) == 89
        save(out / 'audit.json', audit)
        save(out / 'result.json', result)
        save(out / 'status.json', {'stage': 'E3', 'status': 'complete',
                                  'card_coverage': '89/89', 'l2_complete': True})
        (out / 'recovery_summary.md').write_text(
            '# E3 recovered production continuation\n\n'
            '89/89 Skill-aware cards; 267 audited attempts. Task 28 uses recovery9; '
            '88 cards are reused. Two production L2 batches completed with the original '
            'three-candidate selection and closed-set gate. See summary.json, '
            'audit.json and recovery_ledger.json.\n')
    except Exception as exc:
        save(out / 'status.json', {'stage': 'E3', 'status': 'needs_attention',
             'error': str(exc), 'error_class': getattr(exc, 'error_class', type(exc).__name__)})
        raise
    finally:
        if relay is not None:
            relay.close()


if __name__ == '__main__':
    main()
