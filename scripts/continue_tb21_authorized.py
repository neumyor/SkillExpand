"""Continue audited L2 caches with explicit extra-request and correction ledgers."""
import argparse
import json
import logging
import os
import shutil
from pathlib import Path

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.l1.runner import save
from skillexpand.l2.loop import EvolutionConfig, LoopPaths, SerialEvolutionLoop
from skillexpand.persistence.artifacts import load_cold_start, code_signature, provider_signature
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse


def prior_prompts(source, task, body):
    paths = list((source / 'train/usage').glob(f'predicted-*-{task}-{body}.requests.jsonl'))
    assert len(paths) <= 1
    return [row['prompts'][0].removeprefix('Human: ') for row in
            (json.loads(line) for line in paths[0].read_text().splitlines())
            if row.get('event') == 'start'] if paths else []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--stage', required=True, choices=('E3', 'E5'))
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--reset-correction-tasks', type=int, nargs='*', default=[])
    parser.add_argument('--max-tokens', type=int, default=65536)
    parser.add_argument('--accept-output-token-default-change', action='store_true')
    args = parser.parse_args()
    reset_tasks = set(args.reset_correction_tasks)
    assert 1 <= args.max_tokens <= 65536
    assert not reset_tasks or (args.stage == 'E3' and reset_tasks <= {9, 60, 73})
    provider_model = os.environ.get('EXPE_L2_MODEL', 'deepseek-v4-flash-0731-tencent')
    source, out = args.source.resolve(), args.out.resolve()
    source_manifest = json.loads((source / 'l2_manifest.json').read_text())
    current_code = code_signature()
    changed_code = sorted(k for k in set(current_code) | set(source_manifest['code'])
                          if current_code.get(k) != source_manifest['code'].get(k))
    assert not changed_code or (args.accept_output_token_default_change and
        set(changed_code) <= {'runtime/models/llm.py', 'runtime/llm_relay.py'}), changed_code
    source_manifest['code'] = current_code
    assert json.loads((source / 'status.json').read_text())['status'] == 'needs_attention'
    gates = json.loads(Path('runs/tb21-raw-gate-qwen-accepted-20261008/audit.json').read_text())
    assert gates['input_gate_passed'][args.stage]
    if not out.exists():
        shutil.copytree(source, out, ignore=shutil.ignore_patterns(
            'PID', 'run.pid', 'launch.pid', 'campaign.lock',
            'continuation_requests', 'continuation_relay.log'))
        save(out / 'status.json', {'stage': args.stage, 'status': 'prepared'})
        save(out / 'continuation_ledger.json', {
            'authorization': 'user: 把E3 E5 续跑', 'source': str(source),
            'old_runs_read_only': True, 'diagnostic_scores_imported': False,
            'actual_review_workers': args.workers, 'new_l1_requests': 0,
            'input_gate_audit': 'runs/tb21-raw-gate-qwen-accepted-20261008/audit.json',
            'extra_requests': {'20': 1, '71': 1, '81': 1} if args.stage == 'E3' else {},
            'original_correction_budget_reset': bool(reset_tasks),
            'reset_correction_tasks': sorted(reset_tasks),
            'reset_authorization': 'user: E3重置纠正预算' if reset_tasks else None,
            'provider_model': provider_model,
            'max_tokens': args.max_tokens,
            'authorized_code_changes': changed_code,
            'scope': '89-task closed-set acceptance',
        })
    assert json.loads((out / 'status.json').read_text())['status'] == 'prepared'
    cfg, plan, initial, cards = load_cold_start(out)
    if os.environ.get('EXPE_L2_MODEL'):
        for role in ('l2_planner', 'l2_editor', 'l2_reviewer', 'selector'):
            cfg.models[role] = provider_model
    assert len(cards) == 89
    rows = [json.loads(line) for line in (out / 'train/predicted_scores.jsonl').read_text().splitlines()]
    assert len(rows) == len({row['cache_key'] for row in rows})
    if args.dry_run:
        print(json.dumps({'stage': args.stage, 'cache_records': len(rows), 'cards': len(cards),
                          'workers': args.workers, 'status': 'prepared'}))
        return
    reserved = 0
    for path in out.parent.glob('tb21-*/status.json'):
        if path.parent == out:
            continue
        status, _ = json.JSONDecoder().raw_decode(path.read_text())
        pid = status.get('pid')
        if status.get('status') != 'running' or not pid or not Path('/proc', str(pid)).exists():
            continue
        ledger_path = path.parent / 'continuation_ledger.json'
        if not ledger_path.exists():
            ledger_path = path.parent / 'recovery_ledger.json'
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        other_manifest = json.loads((path.parent / 'manifest.json').read_text())
        reserved += int(status.get('workers', ledger.get('actual_review_workers',
                        other_manifest.get('workers', 100))))
    assert 1 <= args.workers <= 100 and reserved + args.workers <= 100, \
        f'Worker ceiling exceeded: {reserved}+{args.workers}'
    os.environ['EXPE_REVIEWER_RESPONSE_FORMAT'] = 'omit'
    os.environ.pop('EXPE_LLM_DISABLE_THINKING', None)
    os.environ['EXPE_LLM_MAX_TOKENS'] = str(args.max_tokens)
    relay = None
    try:
        logging.basicConfig(filename=out / 'continuation_relay.log', level=logging.INFO)
        relay = relay_from_env()
        url = relay.start()
        cfg.benchmark.rollout.relay_base_url = url
        resolved = OmegaConf.to_container(cfg, resolve=True)
        save(out / 'config.json', resolved)
        manifest = json.loads((out / 'manifest.json').read_text())
        manifest['config'] = resolved
        save(out / 'manifest.json', manifest)
        os.environ.update(EXPE_CONFIG_FILE=str(out / 'config.json'), EXPE_TASK_FILE=cfg.benchmark.task_file,
            EXPE_LLM_BASE_URL=url, OPENAI_API_BASE=url, MODEL_API_BASE=url,
            EXPE_LLM_RELAY_REQUIRED='1', TBENCH_RELAY_BASE_URL=url, TBENCH_PERSIST_SANDBOXES='0')
        source_manifest.update(runtime=resolved, provider=provider_signature())
        save(out / 'l2_manifest.json', source_manifest)
        save(out / 'relay_manifest.json', {'transport': 'tencent_e2b_relay',
            'sandbox_id': relay.transport.sandbox_id, 'persistence': 0,
            'direct_provider_fallback': False})
        loop = SerialEvolutionLoop(cfg, plan, LoopPaths(out),
            EvolutionConfig(**source_manifest['config'], evolve_rounds=1))
        scorer = loop._ensure_predicted_scorer()
        scorer.workers = args.workers
        assert all(row['protocol_hash'] == scorer.protocol_hash for row in rows)
        assert not scorer.routes.failed_task_ids
        assert sum(map(len, scorer.routes.groups.values())) == 89
        tasks = {F.task_text_of(cfg, task): task for task in range(89)}
        base_body = S.content_hash(initial[0].body)

        def review(host, prompt):
            payload = json.loads(prompt)
            task = tasks[payload['task']]
            body = S.content_hash(payload['skill']['body'])
            history = prior_prompts(source, task, body)
            used = sum('FORMAT CORRECTION:' in item for item in history)
            evidence = out / 'continuation_requests' / f'{task}-{body}'
            evidence.mkdir(parents=True, exist_ok=True)
            budget = evidence / 'budget.json'
            assert not budget.exists(), 'Identity already requested in this continuation'
            if args.stage == 'E3' and body == base_body and task in (20, 71, 81):
                assert history and used == 3
                save(budget, {'used_before': used, 'extra_authorized_requests': 1,
                    'extra_request_reserved': True, 'corrections_this_run': 0,
                    'max_tokens_sent': True, 'max_tokens': args.max_tokens,
                    'original_budget_reset': False})
                request = {'model': provider_model, 'stream': True,
                    'max_tokens': args.max_tokens,
                    'temperature': 0, 'enable_thinking': True,
                    'messages': [{'role': 'user', 'content': history[-1]}]}
                save(evidence / 'request.json', request)
                with (evidence / 'raw.sse').open('wb') as capture:
                    def chunks():
                        for chunk in relay.transport.stream(request):
                            capture.write(chunk.encode() if isinstance(chunk, str) else chunk)
                            capture.flush()
                            yield chunk
                    completion = coalesce_sse(chunks())
                save(evidence / 'completion.json', completion)
                raw = completion['choices'][0]['message']['content']
                return scorer._parse(raw), len(history) + 1
            if args.stage == 'E3' and task == 60 and body == '7809d5e59cc4':
                used = max(used, 2)
            used_before_reset = used
            if task in reset_tasks:
                used = 0
            reset_details = {'historical_corrections': used_before_reset,
                             'max_tokens': args.max_tokens,
                             'original_budget_reset': task in reset_tasks,
                             'authorization': 'user: E3重置纠正预算' if task in reset_tasks else None}
            remaining = max(0, 3 - used)
            save(budget, {'used_before': used, 'remaining_corrections': remaining,
                          'corrections_this_run': 0, **reset_details})
            current = prompt if not used else prompt + (
                '\n\nFORMAT CORRECTION: Return only the single JSON object; '
                'do not repeat the input or your analysis.')
            for index in range(remaining + 1 if not used else remaining):
                correction = bool(used or index)
                save(budget, {'used_before': used, 'corrections_this_run':
                    index + 1 if used else index, 'original_max_corrections': 3,
                    **reset_details})
                raw = scorer._call(host, current)
                save(evidence / f'response-{index}.json', {'raw': raw, 'correction': correction})
                try:
                    return scorer._parse(raw), used + index + (2 if used else 1)
                except (ValueError, RuntimeError):
                    if index == (remaining if not used else remaining - 1):
                        raise
                    current = prompt + ('\n\nFORMAT CORRECTION: Your visible answer did not '
                        'match the required schema. Return only the single JSON object.')
            raise RuntimeError(f'No remaining reviewer correction budget for task {task}')

        scorer._review = review
        save(out / 'status.json', {'stage': args.stage, 'status': 'running', 'pid': os.getpid(),
            'phase': 'production_L2', 'reused_records': len(rows), 'workers': args.workers})
        (out / 'PID').write_text(str(os.getpid()) + '\n')
        result = loop.run_evolutions()
        assert result['completed_batches'] == result['batches'] == 2
        assert result['invalid_batches'] == 0
        save(out / 'result.json', result)
        save(out / 'audit.json', json.loads((out / 'evolution/round-1/audit.json').read_text()))
        save(out / 'status.json', {'stage': args.stage, 'status': 'complete', 'card_coverage': '89/89'})
        (out / 'continuation_summary.md').write_text(
            f'# {args.stage} continuation\n\nBoth production batches completed and audited. '
            'Closed-set predicted acceptance; no independent test claim. See audit.json and result.json.\n')
    except Exception as exc:
        save(out / 'status.json', {'stage': args.stage, 'status': 'needs_attention',
            'error': str(exc), 'error_class': getattr(exc, 'error_class', type(exc).__name__)})
        raise
    finally:
        if relay is not None:
            relay.close()


if __name__ == '__main__':
    main()
