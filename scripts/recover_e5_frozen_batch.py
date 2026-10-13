"""Resume missing reviewers for the actual frozen E5 first batch."""
import argparse
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.evaluation.validation import PredictedSkillScorer, ScoreCache
from skillexpand.l2.card_review import PROTOCOL
from skillexpand.l2.update import parse_plan
from skillexpand.runtime.llm_relay import TencentSandboxTransport, iter_sse_events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--authorized-extra-request', action='store_true')
    parser.add_argument('--authorized-single-identity-attempts', type=int, default=0,
                        choices=(0, 1, 2, 3))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    root = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
    src = root / 'tb21-e5-qwen-deepseek-mixed-parallel-20261006-recovery3-candidate7'
    recovery_source = os.environ.get('TB21_REVIEWER_RECOVERY_SOURCE', '')
    if args.authorized_extra_request:
        assert recovery_source == 'tb21-e5-20261007-recovery11-format-correction3'
    extra_attempts = args.authorized_single_identity_attempts
    assert not (extra_attempts and args.authorized_extra_request)
    if extra_attempts:
        assert recovery_source == 'tb21-e5-20261007-recovery12-provider-default-output'
    out = root / os.environ.get('TB21_REVIEWER_RECOVERY_RUN', 'tb21-e5-20261007-recovery8-frozen-first-batch')
    cfg = OmegaConf.load(src / 'config.json')
    base = S.from_dict(S.Skill, json.loads((src / 'initial_skills.json').read_text())[0])
    meta = [json.loads(x) for x in (src / 'meta_skills.jsonl').read_text().splitlines()][-1]
    batch = json.loads((src / 'evolution/round-1/batches.json').read_text())[0]
    experiences = [S.from_dict(S.TaskExperience, json.loads((src / f'evolution/round-1/cards/{t}.json').read_text())) for t in batch['task_ids']]
    for exp in experiences:
        assert S.content_hash(S.to_dict(exp)) == batch['card_hashes'][str(exp.task_id)]
    patterns = json.loads((src / f"l2_patterns/{batch['batch_id']}.json").read_text())['patterns']
    route = json.loads((src / 'routes/train/manifest.json').read_text())
    scorer = PredictedSkillScorer(cfg, SimpleNamespace(fingerprint=S.content_hash(route), groups={base.skill_id: tuple(range(89))}), None, workers=8)
    identity = S.content_hash({'protocol': PROTOCOL, 'base': S.to_dict(base), 'cards': [S.to_dict(e) for e in experiences],
        'K': 3, 'meta': meta, 'patterns': patterns, 'skill_edit_mode': 'rewrite', 'acceptance_mode': 'predicted',
        'predicted_review_scope': 'val', 'acceptance_protocol': scorer.protocol_hash})
    proposal = src / 'l2_proposals' / identity
    hypotheses = parse_plan(json.loads((proposal / 'hypotheses.json').read_text())['raw'], experiences, 3)
    candidates = [S.from_dict(S.CandidateSkill, json.loads((proposal / f'candidate-{i}.json').read_text())['candidate']) for i in range(len(hypotheses))]
    panel = f'val:{scorer.routes.fingerprint}:{base.skill_id}'
    skills = [base] + [c.skill for c in candidates]
    accepted = {}
    conflicts = []
    sources = [src / 'train/predicted_scores.jsonl', root / 'tb21-e5-20261006-recovery7-task28-stream/train/predicted_scores.jsonl']
    previous_errors = {}
    correction_counts = {}
    previous_manifest = {}
    if recovery_source:
        previous = root / recovery_source
        sources.append(previous / 'train/predicted_scores.jsonl')
        previous_errors = {item['cache_key']: item for item in json.loads((previous / 'provider_errors.json').read_text())}
        previous_manifest = json.loads((previous / 'recovery_manifest.json').read_text())
        assert previous_manifest['proposal_id'] == identity
        assert previous_manifest['protocol_hash'] == scorer.protocol_hash
        assert len(previous_errors) == int(os.environ.get('TB21_EXPECTED_FAILED_IDENTITIES', '32'))
    for source in sources:
        for line in source.read_text().splitlines():
            row = json.loads(line)
            if row.get('protocol_hash') != scorer.protocol_hash or row.get('panel_key') != panel:
                continue
            if not any(row.get('cache_key') == ScoreCache.make_key('terminalbench', panel, row['task_id'], 'predicted:' + scorer.protocol_hash, s.body) for s in skills):
                continue
            parsed = scorer._parse(json.dumps({k: row[k] for k in ('probability_true', 'predicted_success', 'reason')}))
            if row['cache_key'] in accepted and any(accepted[row['cache_key']][k] != row[k] for k in ('probability_true', 'predicted_success', 'reason')):
                conflicts.append(row['cache_key'])
            accepted.setdefault(row['cache_key'], row)
    assert not conflicts, conflicts
    pending = [(s, t) for s in skills for t in range(89) if ScoreCache.make_key('terminalbench', panel, t, 'predicted:' + scorer.protocol_hash, s.body) not in accepted]
    if recovery_source:
        assert len(pending) == len(previous_errors) and len(accepted) == len(skills) * 89 - len(pending)
        for skill, task in pending:
            key = ScoreCache.make_key('terminalbench', panel, task, 'predicted:' + scorer.protocol_hash, skill.body)
            assert key in previous_errors and previous_errors[key]['error_class'] == 'DiscoveryError'
            usage = src / 'train/usage' / f'predicted-{skill.skill_id}-{task}-{S.content_hash(skill.body)}.requests.jsonl'
            seen = set()
            if usage.exists():
                for line in usage.read_text().splitlines():
                    item = json.loads(line)
                    if item.get('event') == 'start' and 'FORMAT CORRECTION:' in json.dumps(item.get('prompts', [])):
                        seen.add(item['run_id'])
            before = previous_manifest.get('format_corrections_used_before', {}).get(key, len(seen))
            used = before + previous_manifest.get('format_corrections_this_run', 0)
            assert used >= len(seen), f'Format correction accounting mismatch: {key}'
            if args.authorized_extra_request:
                assert used == 3, f'Expected three consumed corrections: {key}'
            elif extra_attempts:
                assert used == 4 and task == 71 and S.content_hash(skill.body) == '5eeb4f5c4423'
            else:
                assert used < 3, f'Format correction budget exhausted: {key}'
            correction_counts[key] = used
    if args.authorized_extra_request:
        assert len(pending) == 7 and len(accepted) == 349
    if extra_attempts:
        assert len(pending) == 1 and len(accepted) == 355
    if args.dry_run:
        print(json.dumps({'reused_records': len(accepted), 'pending_requests': len(pending),
            'pending_identities': [{'task_id': t, 'skill_body_hash': S.content_hash(s.body)}
                                   for s, t in pending],
            'max_tokens_sent': False, 'authorized_extra_requests_per_identity':
            extra_attempts or (1 if args.authorized_extra_request else 0),
            'format_corrections_used_before': correction_counts}, indent=2))
        return
    out.mkdir(exist_ok=False)
    lock = threading.Lock()
    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2) + '\n')
    for name in ('config.json', 'manifest.json', 'split.json', 'initial_skills.json', 'skills.jsonl'):
        shutil.copy2(src / name, out / name)
    save('recovery_manifest.json', {'source_run': str(src), 'batch': batch, 'proposal_id': identity, 'panel_key': panel,
        'protocol_hash': scorer.protocol_hash, 'reused_records': len(accepted), 'pending_requests': len(pending),
        'workers': 8, 'max_workers': 100, 'strict_streaming': True, 'persistence': 0,
        'output_token_limit': 'provider_default', 'max_tokens_sent': False,
        'provider_requests_per_pending_identity': extra_attempts or 1,
        'retry_budget_increased': bool(args.authorized_extra_request or extra_attempts), 'old_runs_read_only': True,
        'authorization': 'User requested one new request per remaining identity without max_tokens'
            if args.authorized_extra_request else
            'User requested multiple retries for the sole missing identity; bounded to three, stop on success'
            if extra_attempts else None,
        'authorized_extra_requests_per_identity': extra_attempts or (1 if args.authorized_extra_request else 0),
        'recovery_source': recovery_source, 'format_corrections_used_before': correction_counts,
        'format_corrections_this_run': 1 if recovery_source else 0,
        'original_max_format_corrections': 3,
        'max_format_corrections': 4 + extra_attempts if extra_attempts else
            4 if args.authorized_extra_request else 3})
    cache = ScoreCache(out / 'train/predicted_scores.jsonl')
    for key, row in accepted.items():
        cache.put(key, row)
    scorer.cache = cache
    state = {'stage': 'E5', 'status': 'running', 'pid': os.getpid(), 'completed_requests': 0, 'failed_requests': 0, 'pending_requests': len(pending), 'proposal_id': identity}
    save('status.json', state)
    (out / 'PID').write_text(str(os.getpid()) + '\n')
    (out / 'recovery_summary.md').write_text('# E5 frozen batch recovery\n\nOnly missing exact reviewer identities for the actual frozen first batch are requested. Historical alternate proposal gates cannot authorize this batch.\n')
    transport = None
    errors = []
    try:
        transport = TencentSandboxTransport(template='sdt-28lb4d9k', api_key=os.environ['OPENAI_API_KEY'],
                    api_base=cfg.benchmark.rollout.provider_base_url, metadata={'x-mounts': '[]'}, request_timeout=300)
        def one(skill, task, request_attempt=1):
            key = ScoreCache.make_key('terminalbench', panel, task, 'predicted:' + scorer.protocol_hash, skill.body)
            prompt = scorer.prompt(route['tasks'][str(task)], skill)
            if recovery_source:
                prompt += ('\n\nFORMAT CORRECTION: your previous visible answer did not '
                           'match the required schema (' + previous_errors[key]['error'] + '). Return only '
                           'the single JSON object now; do not repeat the input or your analysis.')
            folder = out / 'requests' / key
            if extra_attempts:
                folder = folder / f'attempt-{request_attempt}'
            folder.mkdir(parents=True)
            (folder / 'prompt.txt').write_text(prompt)
            metrics = {'task_id': task, 'skill_body_hash': S.content_hash(skill.body), 'cache_key': key,
                'model': cfg.models.l2_reviewer, 'started_at': time.time(), 'chunks': 0,
                'request_attempt': request_attempt}
            try:
                def chunks():
                    for chunk in transport.stream({'model': cfg.models.l2_reviewer, 'stream': True,
                        'messages': [{'role': 'user', 'content': prompt}], 'enable_thinking': True}):
                        metrics['chunks'] += 1
                        metrics.setdefault('first_chunk_at', time.time())
                        with (folder / 'response.sse').open('ab') as fh:
                            fh.write(chunk.encode() if isinstance(chunk, str) else chunk)
                        yield chunk
                content = []
                for event in iter_sse_events(chunks()):
                    choice = (event.get('choices') or [{}])[0]
                    delta = choice.get('delta') or {}
                    if delta.get('content'):
                        metrics.setdefault('first_content_at', time.time())
                        content.append(delta['content'])
                    if choice.get('finish_reason'):
                        metrics['finish_reason'] = choice['finish_reason']
                raw = ''.join(content)
                (folder / 'response.txt').write_text(raw)
                parsed = scorer._parse(raw)
                cache.put(key, {'task_id': task, 'skill_key': skill.key, 'cache_key': key, 'panel_key': panel,
                    'protocol_hash': scorer.protocol_hash, 'format_attempts': correction_counts[key] + request_attempt + 1 if recovery_source else 1, 'response_format': 'omitted',
                    'result_path': str(folder / 'response.txt'), **parsed})
                metrics['status'] = 'ok'
            except Exception as exc:
                metrics.update(status='error', error=str(exc), error_class=getattr(exc, 'error_class', type(exc).__name__))
                with lock:
                    errors.append(metrics.copy())
            finally:
                metrics['ended_at'] = time.time()
                (folder / 'usage.json').write_text(json.dumps(metrics, indent=2) + '\n')
                with lock:
                    state['completed_requests'] += metrics.get('status') == 'ok'
                    state['failed_requests'] += metrics.get('status') != 'ok'
                    save('status.json', state)
            return metrics.get('status') == 'ok'
        if extra_attempts:
            skill, task = pending[0]
            for attempt in range(1, extra_attempts + 1):
                if one(skill, task, attempt):
                    break
                if attempt < extra_attempts:
                    time.sleep(2 ** attempt)
            save('provider_attempt_errors.json', errors)
            errors = list({e['cache_key']: e for e in errors
                           if cache.get(e['cache_key']) is None}.values())
            recovery_manifest = json.loads((out / 'recovery_manifest.json').read_text())
            recovery_manifest['format_corrections_this_run'] = attempt
            recovery_manifest['unused_authorized_requests'] = extra_attempts - attempt
            save('recovery_manifest.json', recovery_manifest)
        else:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(one, skill, task) for skill, task in pending]
                for future in as_completed(futures):
                    future.result()
        save('provider_errors.json', errors)
        validations = []
        if not errors:
            for candidate in candidates:
                validations.append({'candidate_id': candidate.candidate_id, 'result': S.to_dict(scorer.validate(base.skill_id, base, candidate.skill, tuple(range(89)), panel))})
        save('result.json', {'validations': validations, 'candidate_committed': False, 'errors': errors})
        state.update(status='needs_attention', reason='Original batch selection/commit and remaining batch require production L2 continuation' if not errors else 'Missing reviewers retain separate provider errors')
        save('audit.json', {'proposal_id': identity, 'records': len(cache), 'errors': len(errors), 'candidate_gate_executed': bool(validations), 'candidate_committed': False})
        save('status.json', state)
    except Exception as exc:
        state.update(status='needs_attention', error=str(exc), error_class=getattr(exc, 'error_class', type(exc).__name__))
        save('status.json', state)
        raise
    finally:
        if transport is not None:
            transport.close()


if __name__ == '__main__':
    main()
